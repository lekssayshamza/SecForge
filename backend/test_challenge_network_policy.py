"""Offline source-policy checks; no application imports or Terraform execution.

Terraform's own parser is checked separately with fmt/validate. This deliberately
small reader accepts the flat ingress/egress blocks used by these three modules
and fails if their shape changes, rather than silently skipping new permissions.
"""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parent.parent
MODULES = (
    ROOT / "infrastructure/terraform/builder/modules/builder_env",
    ROOT / "infrastructure/terraform/lab/modules/lab_env",
    ROOT / "backend/modules/builder_env",
)


def rules(source, kind):
    starts = re.findall(rf"^\s*{kind}\s*\{{", source, re.MULTILINE)
    bodies = re.findall(rf"^\s*{kind}\s*\{{([^{{}}]*)\}}", source, re.MULTILINE)
    assert len(bodies) == len(starts), "Permission block requires explicit review"
    result = []
    for body in bodies:
        fields = {}
        for line in body.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*(\w+)\s*=\s*(.+?)\s*", line)
            assert match, "Unexpected permission syntax"
            key, value = match.groups()
            assert key not in fields, "Duplicate permission attribute"
            fields[key] = value
        result.append(fields)
    return result


@pytest.mark.parametrize("module", MODULES, ids=("builder", "learner", "legacy-builder"))
def test_management_and_dynamic_room_ports_are_exposed_only_to_configured_vpn(module):
    source = (module / "security_group.tf").read_text()
    ingress = rules(source, "ingress")
    assert len(ingress) == 2
    assert {(r["protocol"], r["from_port"], r["to_port"]) for r in ingress} == {
        ('"tcp"', "22", "22"),
        ('"icmp"', "-1", "-1"),
    }
    for rule in ingress:
        # No IPv6, public CIDR, self reference, SG reference or prefix-list bypass.
        assert set(rule) == {"description", "from_port", "to_port", "protocol", "cidr_blocks"}
        assert rule["cidr_blocks"] == "[var.vpn_cidr]"
    dynamic = re.search(r'dynamic "ingress"\s*\{.*?content\s*\{([^{}]*)\}', source, re.S)
    assert dynamic
    body = dynamic.group(1)
    assert set(re.findall(r'^\s*(\w+)\s*=', body, re.M)) == {'description','from_port','to_port','protocol','cidr_blocks'}
    for field in ('from_port', 'to_port', 'protocol'):
        assert re.search(rf'{field}\s*=\s*ingress.value.{field}',body)
    assert re.search(r'cidr_blocks\s*=\s*\[var.vpn_cidr\]',body)
    assert 'SecForge room ports v1' in body
    assert 'for r in var.exposed_ports' in source
    assert 'ignore_changes = [ingress]' in source


@pytest.mark.parametrize("module", MODULES, ids=("builder", "learner", "legacy-builder"))
def test_existing_outbound_access_is_preserved(module):
    assert rules((module / "security_group.tf").read_text(), "egress") == [{
        "from_port": "0", "to_port": "0", "protocol": '"-1"',
        "cidr_blocks": '["0.0.0.0/0"]',
    }]


@pytest.mark.parametrize("module", MODULES, ids=("builder", "learner", "legacy-builder"))
def test_instances_remain_private_and_use_their_challenge_group(module):
    instance = (module / "instance.tf").read_text()
    assert re.search(r"associate_public_ip_address\s*=\s*false\b", instance)
    role = "lab" if module.name == "lab_env" else "builder"
    assert re.search(
        rf"vpc_security_group_ids\s*=\s*\[aws_security_group\.{role}\.id\]", instance,
    )


@pytest.mark.parametrize("module", MODULES, ids=("builder", "learner", "legacy-builder"))
def test_vpn_source_is_passed_from_existing_root_configuration(module):
    terraform_root = module.parent.parent
    assert re.search(r"vpn_cidr\s*=\s*var\.vpn_cidr\b", (terraform_root / "main.tf").read_text())
    for variables in (terraform_root / "variables.tf", module / "variables.tf"):
        block = re.search(r'variable "vpn_cidr"\s*\{(.*?)\n\}', variables.read_text(), re.S)
        assert block
        assert re.search(r'default\s*=\s*"10\.8\.0\.0/24"', block.group(1))
        assert 'private IPv4 VPN CIDR' in block.group(1)
        assert re.search(r'variable "exposed_ports"', variables.read_text())
        assert re.search(r'protocol\s*=\s*"tcp", from_port = 80, to_port = 80', variables.read_text())
    assert re.search(r'exposed_ports\s*=\s*var.exposed_ports', (terraform_root / 'main.tf').read_text())


def test_active_builder_and_learner_ingress_policies_match():
    assert rules((MODULES[0] / "security_group.tf").read_text(), "ingress") == rules(
        (MODULES[1] / "security_group.tf").read_text(), "ingress",
    )
