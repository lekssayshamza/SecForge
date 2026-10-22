"""Synthetic gateway snapshots only; external processes are denied by default."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import inspect_vpn_gateway as inspection


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("External process attempted")))


@pytest.fixture
def nft_data():
    return {"nftables": [
        {"chain": {"family": "inet", "table": "filter", "name": "forward", "hook": "forward", "type": "filter", "policy": "drop", "prio": 0}},
        {"chain": {"family": "inet", "table": "filter", "name": "private-owner-chain"}},
        {"chain": {"family": "inet", "table": "filter", "name": "input", "hook": "input", "policy": "drop"}},
        {"rule": {"family": "inet", "table": "filter", "chain": "forward", "expr": [
            {"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": ["established", "related"]}}, {"accept": None}]}},
        {"rule": {"family": "inet", "table": "filter", "chain": "forward", "comment": "secforge-vpn-authz", "expr": [
            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "tun0"}},
            {"match": {"op": "==", "left": {"concat": [{"payload": {"protocol": "ip", "field": "saddr"}}, {"payload": {"protocol": "ip", "field": "daddr"}}]},
                       "right": {"set": [{"concat": ["10.8.0.65", "10.0.1.120"]}]}}},
            {"accept": None}]}},
        {"rule": {"family": "inet", "table": "filter", "chain": "forward", "expr": [{"jump": {"target": "private-owner-chain"}}]}},
        {"rule": {"family": "inet", "table": "filter", "chain": "private-owner-chain", "comment": "FAKE_SECRET", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 80}}, {"drop": None}]}},
        {"rule": {"family": "inet", "table": "filter", "chain": "input", "expr": [{"log": {"prefix": "FAKE_SECRET"}}]}},
        {"set": {"family": "inet", "table": "secforge-vpn", "name": "lab_pairs", "type": ["ipv4_addr", "ipv4_addr"],
                 "elem": [{"concat": ["10.8.0.65", "10.0.1.120"]}]}},
    ]}


def test_current_forward_structure_and_correlations_survive_sanitization(nft_data):
    report = inspection.Collector().nft_report(nft_data)
    assert report["available"] and report["expected_secforge_forward_chain_present"]
    assert report["secforge_marked_rule_count"] == report["marked_rules_in_expected_forward_chain"] == 1
    assert report["snat_or_masquerade_present"] is False
    assert len(report["chains"]) == 2  # Forward chain and its reachable jump target, not input.
    forward, child = report["chains"]
    assert forward["policy"] == "drop" and forward["priority"] == 0
    assert forward["rules"][-1]["expressions"][0]["jump"]["target"] == child["name_label"]
    assert forward["table_label"] == child["table_label"]
    assert '"right": 80' in json.dumps(child)
    serialized = json.dumps(report)
    assert "tun0" in serialized and "vpn-host-1" in serialized and "challenge-host-1" in serialized
    for forbidden in ("10.8.0.65", "10.0.1.120", "private-owner-chain", "FAKE_SECRET"):
        assert forbidden not in serialized
    pair = report["sets"][0]["elements"][0]["concat"]
    assert pair == ["vpn-host-1", "challenge-host-1"]


@pytest.mark.parametrize("verb", ["snat", "masquerade"])
def test_nat_in_a_reachable_jump_chain_is_not_missed(nft_data, verb):
    nft_data["nftables"].extend([
        {"chain": {"family": "ip", "table": "nat", "name": "postrouting", "type": "nat", "hook": "postrouting", "prio": 100, "policy": "accept"}},
        {"chain": {"family": "ip", "table": "nat", "name": "private-nat-chain"}},
        {"rule": {"family": "ip", "table": "nat", "chain": "postrouting", "expr": [{"goto": {"target": "private-nat-chain"}}]}},
        {"rule": {"family": "ip", "table": "nat", "chain": "private-nat-chain", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": {"prefix": {"addr": "10.8.0.0", "len": 24}}}},
            {verb: {"addr": "10.0.1.9"}}]}},
    ])
    report = inspection.Collector().nft_report(nft_data)
    assert report["snat_or_masquerade_present"] is True
    assert len(report["chains"]) == 4
    assert "10.0.1.9" not in json.dumps(report)
    # Prefix representation must remain meaningful, not turn its address into a host alias.
    assert '"network": "10.8.0.0/24"' in json.dumps(report)


def test_rule_cycles_terminate_and_unknown_expressions_are_flagged(nft_data):
    nft_data["nftables"].append({"rule": {"family": "inet", "table": "filter", "chain": "private-owner-chain", "expr": [
        {"goto": {"target": "forward"}}, {"FAKE_SECRET_KEY": "FAKE_SECRET_VALUE"}]}})
    serialized = json.dumps(inspection.Collector().nft_report(nft_data))
    assert "unsupported_expression_present" in serialized
    assert "FAKE_SECRET" not in serialized


def test_openvpn_routing_only_no_embedded_material_or_host_addresses():
    text = '''dev tun0
proto udp
port 1194
topology subnet
server 10.8.0.0 255.255.255.0
push "route 10.0.1.0 255.255.255.0"
route-gateway 10.8.0.1
client-to-client
ca /private/FAKE_SECRET.pem
auth-user-pass /private/FAKE_SECRET
<key>
route 203.0.113.42 255.255.255.255
FAKE_PRIVATE_KEY
</key>
'''
    rows = inspection.Collector().directives(text)
    by_name = {r["directive"]: r["arguments"] for r in rows}
    assert by_name["server"] == ["10.8.0.0/24"]
    assert by_name["push route"] == ["10.0.1.0/24"]
    assert by_name["port"] == [1194]
    assert by_name["dev"] == ["tun0"]
    assert by_name["route-gateway"] == ["arguments-withheld"]
    assert "client-to-client" in by_name
    assert "FAKE_" not in json.dumps(rows) and "203.0.113.42" not in json.dumps(rows)
    assert "10.8.0.1" not in json.dumps(rows)


@pytest.mark.parametrize("path", ["/tmp/server.conf", "/etc/openvpn/pki/server.conf", "/etc/openvpn/server/private.key", "/etc/openvpn/ccd/user.conf"])
def test_configuration_path_restrictions_reject_before_read(monkeypatch, path):
    read = Mock(side_effect=AssertionError("Unexpected config read"))
    monkeypatch.setattr(Path, "read_text", read)
    assert inspection.Collector().config(Path(path))["readable"] is False
    read.assert_not_called()


def test_persistence_hints_do_not_claim_boot_equivalence():
    result = inspection.Collector.persistence_structure('# masquerade\nchain f { type filter hook forward priority 0; policy drop; }\ninclude "/private/name"\n')
    assert result == {"forward_hook_text_present": True, "snat_or_masquerade_text_present": False,
                      "secforge_marker_text_present": False, "include_directives_present": True}


@pytest.mark.parametrize("check", list(inspection.COMMANDS))
def test_only_fixed_read_only_commands_execute_with_bounds(monkeypatch, check):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    collector = inspection.Collector()
    assert collector.run(check) == ""
    args, kwargs = run.call_args
    assert args[0] == inspection.COMMANDS[check]
    assert kwargs["shell"] is False and 0 < kwargs["timeout"] <= 8
    assert "restart" not in args[0] and "-f" not in args[0]


@pytest.mark.parametrize("check,unit", [("restart", None), ("unit", "ssh.service"), ("unit", "openvpn@server.service;unsafe"), ("nft", "extra")])
def test_non_allowlisted_operations_are_rejected(check, unit):
    with pytest.raises(ValueError, match="allowlisted"):
        inspection.Collector().run(check, unit)


@pytest.mark.parametrize("failure", [FileNotFoundError("FAKE_SECRET"), subprocess.TimeoutExpired("FAKE_SECRET", 8), PermissionError("FAKE_SECRET")])
def test_command_errors_are_sanitized(monkeypatch, failure):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    collector = inspection.Collector()
    assert collector.run("nft") is None
    assert "FAKE_SECRET" not in json.dumps(collector.errors)


def test_total_deadline_prevents_further_commands(monkeypatch):
    collector = inspection.Collector()
    collector.deadline = 0
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert collector.run("nft") is None
    run.assert_not_called()
    assert collector.errors == [{"check": "nft", "reason": "total-timeout"}]


def test_collection_resolves_service_working_directory_and_keeps_failures_explicit(monkeypatch, nft_data):
    responses = {"services": "openvpn-server@server.service loaded active running PRIVATE_DESCRIPTION\n",
        "unit": "ActiveState=active\nSubState=running\nUnitFileState=enabled\nWorkingDirectory=/etc/openvpn/server\nExecStart={ argv[]=/usr/sbin/openvpn --config server.conf ; }\n",
        "forwarding": "1\n", "nft": json.dumps(nft_data),
        "routes": json.dumps([{"dst": "10.8.0.0/24", "dev": "tun0"}, {"dst": "default", "gateway": "203.0.113.7", "dev": "private-interface"}]),
        "nft_service": "ActiveState=active\nUnitFileState=enabled\n",
        "legacy_nat": None, "legacy_filter": ":FORWARD DROP [0:0]\n-A FORWARD -p tcp --dport 22 -j ACCEPT\n"}
    collector = inspection.Collector()
    monkeypatch.setattr(collector, "run", lambda name, unit=None: responses[name])
    config = Mock(return_value={"readable": True, "directives": []})
    monkeypatch.setattr(collector, "config", config)
    monkeypatch.setattr(collector, "persistence_files", lambda: {"nft_config_on_disk": {"readable": False}})
    report = collector.collect()
    config.assert_called_once_with(Path("/etc/openvpn/server/server.conf"))
    assert report["ipv4_forwarding"] is True
    assert report["openvpn_services"][0]["active_state"] == "active"
    assert report["legacy_iptables"]["nat"]["snat_or_masquerade_present"] is None
    assert report["legacy_iptables"]["filter"]["rules"][1] == ["-A", "FORWARD", "-p", "tcp", "--dport", 22, "-j", "ACCEPT"]
    serialized = json.dumps(report)
    for forbidden in ("PRIVATE_DESCRIPTION", "server.conf", "203.0.113.7", "private-interface"):
        assert forbidden not in serialized


@pytest.mark.parametrize("args", [[], ["--allow-read-only-gateway", "--unsafe", "FAKE_SECRET"]])
def test_opt_in_and_errors_do_not_expose_arguments(args, capsys):
    with pytest.raises(SystemExit) as caught:
        inspection.main(args)
    assert caught.value.code == 2
    assert "FAKE_SECRET" not in capsys.readouterr().err


def test_no_elevated_access_fails_before_collection(monkeypatch, capsys):
    monkeypatch.setattr(inspection.os, "geteuid", lambda: 1000)
    assert inspection.main(["--allow-read-only-gateway"]) == 1
    assert "elevated" in capsys.readouterr().err


def test_main_writes_only_sanitized_json(monkeypatch, capsys):
    monkeypatch.setattr(inspection.os, "geteuid", lambda: 0)
    monkeypatch.setattr(inspection.Collector, "collect", lambda self: {"available": True, "nftables": self.nft_report({"nftables": []})})
    assert inspection.main(["--allow-read-only-gateway"]) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True
