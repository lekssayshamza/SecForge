"""Synthetic responses only. No AWS processes, credentials or application imports."""

import copy
import json
import stat
import subprocess
from unittest.mock import Mock

import pytest

import inspect_aws_network as inspection


@pytest.fixture(autouse=True)
def deny_external_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A test attempted an external process")
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.fixture
def inventory():
    gateway = "i-00000000000000001"
    lab = "i-00000000000000002"
    vpc, subnet = "vpc-00000001", "subnet-00000001"
    gateway_group, lab_group = "sg-00000001", "sg-00000002"
    eni = "eni-00000001"
    return {
        "describe-instances": {"Reservations": [{"OwnerId": "123456789012", "Instances": [
            {"InstanceId": gateway, "State": {"Name": "running"}, "VpcId": vpc,
             "SubnetId": subnet, "SourceDestCheck": False,
             "PublicIpAddress": "203.0.113.9", "PrivateIpAddress": "10.0.1.9",
             "Tags": [{"Key": "Name", "Value": "private-name-openvpn"},
                      {"Key": "Secret", "Value": "FAKE_SECRET_NOT_FOR_REPORT"}],
             "SecurityGroups": [{"GroupId": gateway_group}],
             "NetworkInterfaces": [{"NetworkInterfaceId": eni}]},
            {"InstanceId": lab, "State": {"Name": "running"}, "VpcId": vpc,
             "SubnetId": subnet, "SourceDestCheck": True,
             "Tags": [{"Key": "ResourceType", "Value": "lab"}],
             "SecurityGroups": [{"GroupId": lab_group}]},
        ]}]},
        "describe-security-groups": {"SecurityGroups": [
            {"GroupId": gateway_group, "VpcId": vpc, "GroupName": "secret-gateway-name",
             "OwnerId": "123456789012", "Description": "FAKE_SECRET_NOT_FOR_REPORT",
             "IpPermissions": [{"IpProtocol": "udp", "FromPort": 1194, "ToPort": 1194,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
            {"GroupId": lab_group, "VpcId": vpc,
             "IpPermissions": [
                 {"IpProtocol": "6", "FromPort": 22, "ToPort": 22,
                  "IpRanges": [{"CidrIp": "10.8.0.0/24", "Description": "FAKE_SECRET_NOT_FOR_REPORT"}]},
                 {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                  "UserIdGroupPairs": [{"GroupId": gateway_group, "VpcId": vpc, "UserId": "123456789012"}]},
                 {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1,
                  "IpRanges": [{"CidrIp": "10.0.1.9/32"}, {"CidrIp": "203.0.113.10/32"}],
                  "Ipv6Ranges": [{"CidrIpv6": "2001:db8::9/128"}, {"CidrIpv6": "2001:db8:abcd::/64"}]},
             ],
             "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
        ]},
        "describe-vpcs": {"Vpcs": [{"VpcId": vpc, "CidrBlock": "10.0.0.0/16"}]},
        "describe-subnets": {"Subnets": [{"SubnetId": subnet, "VpcId": vpc,
            "CidrBlock": "10.0.1.0/24", "MapPublicIpOnLaunch": False}]},
        "describe-route-tables": {"RouteTables": [{"RouteTableId": "rtb-00000001", "VpcId": vpc,
            "Associations": [{"Main": True}], "Routes": [
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                {"DestinationCidrBlock": "10.8.0.0/24", "NetworkInterfaceId": eni, "State": "active"},
                {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-00000001", "State": "active"},
                {"DestinationIpv6CidrBlock": "::/0", "GatewayId": "igw-00000001", "State": "blackhole"},
                {"DestinationCidrBlock": "192.0.2.0/24", "CoreNetworkArn": "arn:aws:networkmanager::123456789012:core-network/fake", "State": "active"},
            ]}]},
        "describe-network-interfaces": {"NetworkInterfaces": [{"NetworkInterfaceId": eni,
            "Attachment": {"InstanceId": gateway}, "VpcId": vpc, "SubnetId": subnet,
            "Groups": [{"GroupId": gateway_group}], "Status": "in-use", "SourceDestCheck": False,
            "PrivateIpAddress": "10.0.1.9", "Association": {"PublicIp": "203.0.113.9"},
            "Ipv6Addresses": [{"Ipv6Address": "2001:db8::9"}]}]},
    }


def test_sensitive_fields_and_host_addresses_never_escape(inventory):
    serialized = json.dumps(inspection.sanitize_inventory(inventory))
    forbidden = ["123456789012", "arn:aws:", "203.0.113.9", "203.0.113.10", "10.0.1.9",
                 "2001:db8::9", "FAKE_SECRET_NOT_FOR_REPORT", "private-name", "secret-gateway-name"]
    for rows in inventory.values():
        # Check every synthetic AWS identifier without printing a raw response.
        import re
        forbidden.extend(re.findall(r"(?:i|eni|sg|vpc|subnet|rtb|nat|igw)-[0-9a-f]+", json.dumps(rows)))
    assert all(value not in serialized for value in forbidden)
    assert "10.8.0.0/24" in serialized
    assert "10.0.1.0/24" in serialized
    assert "2001:db8:abcd::/64" in serialized


def test_relationships_ports_public_exposure_and_route_roles_preserved(inventory):
    report = inspection.sanitize_inventory(inventory)
    gateway, lab = report["instances"]
    assert gateway["role_hint"] == "gateway-candidate"
    assert gateway["has_public_ip"] is True and gateway["source_dest_check"] is False
    assert lab["role_hint"] == "challenge-lab" and lab["has_public_ip"] is False
    assert lab["running_builder_or_lab_charge_candidate"] is True
    assert gateway["subnet"] == lab["subnet"] == report["subnets"][0]["label"]
    assert report["subnets"][0]["association"] == "inherited-main"
    assert report["subnets"][0]["route_tables"] == [report["route_tables"][0]["label"]]
    assert report["network_interfaces"][0]["instance"] == gateway["label"]
    gateway_rule = report["security_groups"][0]["ingress"][0]
    assert gateway_rule["protocol"] == "udp" and gateway_rule["from_port_or_icmp_type"] == 1194
    assert gateway_rule["peers"] == [{"network": "0.0.0.0/0"}]
    ssh, http, icmp = report["security_groups"][1]["ingress"]
    assert ssh["protocol"] == "tcp" and ssh["from_port_or_icmp_type"] == 22
    assert http["from_port_or_icmp_type"] == 80
    assert http["peers"][0]["group"] == gateway["groups"][0]
    assert http["peers"][0]["group_in_inventory"] is True
    assert icmp["peers"][0]["network"] == f"host-of-{gateway['label']} (/32)"
    assert icmp["peers"][2]["network"] == f"host-of-{gateway['label']} (/128)"
    routes = report["route_tables"][0]["routes"]
    assert routes[1]["target"] == {"resource": gateway["interfaces"][0], "instance": gateway["label"], "role_hint": "gateway-candidate"}
    assert routes[2]["target"]["resource"] == "nat-gateway-1"
    assert routes[3]["state"] == "blackhole" and routes[3]["target"]["resource"] == "internet-gateway-1"
    assert routes[4]["target"]["resource"] == "core-network-1"


def test_explicit_subnet_route_table_wins_over_main(inventory):
    inventory["describe-route-tables"]["RouteTables"].append({
        "RouteTableId": "rtb-00000002", "VpcId": "vpc-00000001", "Routes": [],
        "Associations": [{"SubnetId": "subnet-00000001", "AssociationState": {"State": "associated"}}],
    })
    subnet = inspection.sanitize_inventory(inventory)["subnets"][0]
    assert subnet["association"] == "explicit" and subnet["route_tables"] == ["route-table-2"]
    inventory["describe-route-tables"]["RouteTables"][1]["Associations"][0]["AssociationState"]["State"] = "disassociated"
    assert inspection.sanitize_inventory(inventory)["subnets"][0]["association"] == "inherited-main"


def test_empty_inventory_and_unknown_fields_fail_closed(inventory):
    empty = {op: {key: []} for op, key in inspection.OPERATIONS.items()}
    assert inspection.sanitize_inventory(empty)["instances"] == []
    inventory["describe-instances"]["Reservations"][0]["Instances"][0]["State"]["Name"] = "SECRET_MARKER"
    inventory["describe-security-groups"]["SecurityGroups"][1]["IpPermissions"][0]["IpRanges"][0]["CidrIp"] = "SECRET_MARKER"
    inventory["describe-security-groups"]["SecurityGroups"][1]["IpPermissions"][0]["IpProtocol"] = "SECRET_MARKER"
    assert "SECRET_MARKER" not in json.dumps(inspection.sanitize_inventory(inventory))


@pytest.mark.parametrize("tags,expected", [
    ([{"Key": "Name", "Value": "builder-legacy"}], "builder"),
    ([{"Key": "Name", "Value": "lab-legacy"}], "challenge-lab"),
    ([{"Key": "Environment", "Value": "builder"}], "builder"),
    ([{"Key": "Environment", "Value": "learner-lab"}], "challenge-lab"),
    ([{"Key": "Secret", "Value": "vpn-password"}], "unclassified"),
])
def test_roles_use_only_expected_tag_hints(tags, expected):
    assert inspection.role_hint({"Tags": tags}) == expected


def test_foreign_group_prefix_list_and_pending_resources_are_visible(inventory):
    lab = inventory["describe-instances"]["Reservations"][0]["Instances"][1]
    lab["State"]["Name"] = "pending"
    group = inventory["describe-security-groups"]["SecurityGroups"][1]
    group["IpPermissions"].append({"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "UserIdGroupPairs": [{"GroupId": "sg-00000099", "VpcId": "vpc-00000099", "UserId": "999999999999"}],
        "PrefixListIds": [{"PrefixListId": "pl-00000001"}]})
    report = inspection.sanitize_inventory(inventory)
    assert report["instances"][1]["running_builder_or_lab_charge_candidate"] is True
    peers = report["security_groups"][1]["ingress"][-1]["peers"]
    assert peers == [{"group": "group-3", "vpc": "vpc-2", "group_in_inventory": False}, {"prefix_list": "prefix-list-1"}]
    assert "999999999999" not in json.dumps(report) and "pl-00000001" not in json.dumps(report)


@pytest.mark.parametrize("key,value,kind", [
    ("InstanceId", "i-00000000000000001", "instance-1"),
    ("TransitGatewayId", "tgw-00000001", "transit-gateway-1"),
    ("VpcPeeringConnectionId", "pcx-00000001", "peering-1"),
    ("EgressOnlyInternetGatewayId", "eigw-00000001", "egress-only-gateway-1"),
    ("GatewayId", "vgw-00000001", "virtual-gateway-1"),
    ("GatewayId", "vpce-00000001", "vpc-endpoint-1"),
])
def test_other_route_targets_are_anonymized_without_losing_kind(inventory, key, value, kind):
    inventory["describe-route-tables"]["RouteTables"][0]["Routes"].append({
        "DestinationCidrBlock": "198.51.100.0/24", key: value, "State": "active"})
    report = inspection.sanitize_inventory(inventory)
    target = report["route_tables"][0]["routes"][-1]["target"]
    assert target["resource"] == kind
    assert value not in json.dumps(report)


def test_labels_repeat_consistently_and_order_is_stable(inventory):
    first = inspection.sanitize_inventory(inventory)
    changed = copy.deepcopy(inventory)
    changed["describe-instances"]["Reservations"][0]["Instances"].reverse()
    changed["describe-security-groups"]["SecurityGroups"].reverse()
    second = inspection.sanitize_inventory(changed)
    first.pop("collected_at_utc")
    second.pop("collected_at_utc")
    assert first == second


@pytest.mark.parametrize("operation", list(inspection.OPERATIONS))
def test_cli_is_read_only_explicit_bounded_and_keeps_pagination(monkeypatch, inventory, operation):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(inventory[operation]), ""))
    monkeypatch.setattr(subprocess, "run", run)
    assert inspection.read_aws(operation) == inventory[operation]
    args, options = run.call_args
    command = args[0]
    assert command[:3] == ["aws", "ec2", operation]
    assert command[command.index("--region") + 1] == "eu-north-1"
    assert command[command.index("--endpoint-url") + 1] == "https://ec2.eu-north-1.amazonaws.com"
    assert "--no-paginate" not in command and "--max-items" not in command
    assert "--debug" not in command
    assert options["timeout"] == 35 and options["shell"] is False
    assert options["env"]["AWS_MAX_ATTEMPTS"] == "1"
    assert options["env"]["AWS_EC2_METADATA_DISABLED"] == "true"


def test_non_allowlisted_operation_is_rejected_before_process():
    with pytest.raises(inspection.InspectionError, match="allowlist"):
        inspection.read_aws("authorize-security-group-ingress")


@pytest.mark.parametrize("stderr", ["Could not connect to endpoint SECRET_MARKER", "UnauthorizedOperation SECRET_MARKER",
                                    "Unable to locate credentials SECRET_MARKER", "ExpiredToken SECRET_MARKER", "other SECRET_MARKER"])
def test_aws_errors_never_expose_raw_diagnostics(monkeypatch, stderr):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 255, "SECRET_MARKER", stderr))
    with pytest.raises(inspection.InspectionError) as caught:
        inspection.read_aws("describe-instances")
    assert "SECRET_MARKER" not in str(caught.value)


@pytest.mark.parametrize("response", ["SECRET_MARKER", "[]", "{}", '{"Reservations": "SECRET_MARKER"}'])
def test_invalid_responses_do_not_expose_raw_data(monkeypatch, response):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, response, ""))
    with pytest.raises(inspection.InspectionError) as caught:
        inspection.read_aws("describe-instances")
    assert "SECRET_MARKER" not in str(caught.value)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("SECRET_MARKER", 35, output="SECRET_MARKER"), FileNotFoundError("SECRET_MARKER")])
def test_timeout_and_missing_cli_are_sanitized(monkeypatch, error):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    with pytest.raises(inspection.InspectionError) as caught:
        inspection.read_aws("describe-instances")
    assert "SECRET_MARKER" not in str(caught.value)


@pytest.mark.parametrize("args", [[], ["--output", "SECRET_MARKER"], ["--allow-read-only-aws", "--region", "SECRET_MARKER"]])
def test_opt_in_is_required_and_bad_arguments_are_not_echoed(args, capsys):
    with pytest.raises(SystemExit) as caught:
        inspection.main(args)
    assert caught.value.code == 2
    assert "SECRET_MARKER" not in capsys.readouterr().err


def test_main_writes_only_sanitized_private_report(monkeypatch, tmp_path, inventory, capsys):
    calls = []
    def fake(operation):
        calls.append(operation)
        return inventory[operation]
    monkeypatch.setattr(inspection, "read_aws", fake)
    output = tmp_path / "report.json"
    assert inspection.main(["--allow-read-only-aws", "--output", str(output)]) == 0
    assert calls == list(inspection.OPERATIONS)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "FAKE_SECRET_NOT_FOR_REPORT" not in output.read_text()
    assert json.loads(output.read_text())["region"] == "eu-north-1"
    assert "FAKE_SECRET_NOT_FOR_REPORT" not in capsys.readouterr().out


@pytest.mark.parametrize("symlink", [False, True])
def test_refuses_existing_files_and_symlinks_without_aws(monkeypatch, tmp_path, symlink):
    original = tmp_path / "preserve"
    original.write_text("preserve")
    output = tmp_path / "report.json" if symlink else original
    if symlink:
        output.symlink_to(original)
    aws = Mock()
    monkeypatch.setattr(inspection, "read_aws", aws)
    assert inspection.main(["--allow-read-only-aws", "--output", str(output)]) == 1
    aws.assert_not_called()
    assert original.read_text() == "preserve"


def test_output_creation_race_preserves_existing_file(monkeypatch, tmp_path, inventory):
    output = tmp_path / "report.json"
    def fake(operation):
        if not output.exists():
            output.write_text("preserve")
        return inventory[operation]
    monkeypatch.setattr(inspection, "read_aws", fake)
    assert inspection.main(["--allow-read-only-aws", "--output", str(output)]) == 1
    assert output.read_text() == "preserve"


def test_failed_inventory_writes_no_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(inspection, "read_aws", Mock(side_effect=inspection.InspectionError("AWS endpoint unavailable.")))
    output = tmp_path / "report.json"
    assert inspection.main(["--allow-read-only-aws", "--output", str(output)]) == 1
    assert not output.exists()
    assert "endpoint unavailable" in capsys.readouterr().err


def test_later_inventory_failure_never_writes_partial_raw_report(monkeypatch, tmp_path, inventory, capsys):
    calls = []
    def fake(operation):
        calls.append(operation)
        if operation == "describe-subnets":
            raise inspection.InspectionError("AWS describe permission denied.")
        return inventory[operation]
    monkeypatch.setattr(inspection, "read_aws", fake)
    output = tmp_path / "report.json"
    assert inspection.main(["--allow-read-only-aws", "--output", str(output)]) == 1
    assert calls == list(inspection.OPERATIONS)[:4]
    assert not output.exists()
    assert "FAKE_SECRET_NOT_FOR_REPORT" not in capsys.readouterr().err
