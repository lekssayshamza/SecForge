"""Run on the existing gateway via approved access; read-only, sanitized stdout."""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


COMMANDS = {
    "services": ["systemctl", "list-units", "--all", "--type=service", "--plain", "--no-legend", "openvpn*.service"],
    "forwarding": ["sysctl", "-n", "net.ipv4.ip_forward"],
    "nft": ["nft", "-j", "list", "ruleset"],
    "routes": ["ip", "-j", "-4", "route", "show", "table", "all"],
    "nft_service": ["systemctl", "show", "nftables.service", "--property=ActiveState,UnitFileState"],
    "legacy_nat": ["iptables-legacy-save", "-t", "nat"],
    "legacy_filter": ["iptables-legacy-save", "-t", "filter"],
}
UNIT = re.compile(r"openvpn(?:-server)?(?:@[A-Za-z0-9_.-]+)?\.service")
SAFE_WORDS = set("inet ip ip6 tcp udp icmp icmpv6 ipv4_addr ipv6_addr ether saddr daddr sport dport iifname oifname iif oif mark l4proto nfproto protocol state established related new invalid untracked tun0 tun1 accept drop reject return == != in & | ^ << >> forward prerouting postrouting filter nat input output default link host global unicast local kernel boot static enabled disabled static indirect masked active inactive failed running exited dead not-found activating deactivating def1 bypass-dhcp bypass-dns block-local ipv6 client server subnet net30 p2p tun tap udp4 udp6 tcp-server tcp4-server tcp6-server".split())
EXPR_KEYS = set("match left right op payload protocol field base offset len meta key ct prefix addr length set concat range elem value verdict accept drop reject return jump goto target snat dnat masquerade redirect flags family random fully-random persistent port num type map data vmap name".split())
SAFE_WORDS.update("-A -j -g -p -s -d -i -o -m --sport --dport --sports --dports --ctstate --state --to-source --to-destination --jump --goto --tcp-flags --comment FORWARD :FORWARD POSTROUTING :POSTROUTING PREROUTING :PREROUTING ACCEPT DROP REJECT RETURN SNAT MASQUERADE DNAT tcp udp icmp conntrack state enabled-runtime masked-runtime".split())
SAFE_WORDS.update({"arguments-withheld", "invalid-network", "invalid-port"})


class Collector:
    def __init__(self):
        self.deadline = time.monotonic() + 120
        self.names = {}
        self.errors = []

    def label(self, kind, value):
        key = (kind, str(value))
        if key not in self.names:
            number = 1 + sum(k[0] == kind for k in self.names)
            self.names[key] = f"{kind}-{number}"
        return self.names[key]

    def sanitize(self, value):
        if isinstance(value, dict):
            output = {k: self.sanitize(v) for k, v in value.items() if k in EXPR_KEYS}
            if isinstance(value.get("prefix"), dict):
                prefix = value["prefix"]
                try:
                    network = ipaddress.ip_network((prefix.get("addr"), prefix.get("len")), strict=False)
                    output["prefix"] = {"network": self.sanitize(str(network))}
                except (ValueError, TypeError):
                    output["unsupported_prefix_present"] = True
            if any(k not in EXPR_KEYS and k not in ("comment", "counter", "log") for k in value):
                output["unsupported_expression_present"] = True
            return output
        if isinstance(value, list):
            return [self.sanitize(v) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return value if -65535 <= value <= 65535 else "number-withheld"
        if isinstance(value, str):
            if value.isdigit() and len(value) <= 5 and int(value) <= 65535:
                return int(value)
            try:
                network = ipaddress.ip_network(value, strict=False)
                if network.prefixlen != network.max_prefixlen:
                    return str(network)
                address = network.network_address
                kind = "vpn-host" if address in ipaddress.ip_network("10.8.0.0/24") else "challenge-host" if address in ipaddress.ip_network("10.0.1.0/24") else "host"
                return self.label(kind, address)
            except ValueError:
                return value if value in SAFE_WORDS else self.label("symbol", value.lstrip("@"))
        return "unsupported-value"

    def run(self, name, unit=None):
        if name == "unit" and unit and UNIT.fullmatch(unit):
            command = ["systemctl", "show", unit, "--property=ActiveState,SubState,UnitFileState,ExecStart,WorkingDirectory"]
        elif name in COMMANDS and unit is None:
            command = COMMANDS[name]
        else:
            raise ValueError("Non-allowlisted read operation")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.errors.append({"check": name, "reason": "total-timeout"})
            return None
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=min(8, remaining), shell=False,
                                    env=dict(os.environ, SYSTEMD_PAGER="", SYSTEMD_COLORS="0", LC_ALL="C"))
        except FileNotFoundError:
            self.errors.append({"check": name, "reason": "tool-unavailable"})
            return None
        except (OSError, subprocess.TimeoutExpired):
            self.errors.append({"check": name, "reason": "unavailable-or-timeout"})
            return None
        if result.returncode:
            self.errors.append({"check": name, "reason": "read-denied-or-unavailable"})
            return None
        return result.stdout

    def json_command(self, name):
        text = self.run(name)
        try:
            return json.loads(text) if text is not None else None
        except (TypeError, ValueError):
            self.errors.append({"check": name, "reason": "invalid-json"})
            return None

    def config(self, path):
        """Read only server .conf paths under /etc/openvpn, never referenced PKI files."""
        try:
            resolved = path.resolve()
            if resolved.suffix != ".conf" or not resolved.is_relative_to(Path("/etc/openvpn")):
                return {"readable": False, "reason": "config-path-not-allowlisted"}
            # Do not follow symlinks into certificate/private-key locations.
            if any(word in resolved.parts for word in ("pki", "ccd", "easy-rsa")):
                return {"readable": False, "reason": "config-path-not-allowlisted"}
            return {"readable": True, "directives": self.directives(resolved.read_text())}
        except (OSError, ValueError):
            return {"readable": False, "reason": "config-unavailable"}

    def directives(self, text):
        output = []
        inline_block = False
        for line in text.splitlines():
            if line.strip().startswith("</"):
                inline_block = False
                continue
            if line.strip().startswith("<"):
                inline_block = True
                continue
            if inline_block:
                continue
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not tokens:
                continue
            if tokens[0] == "push" and len(tokens) == 2:
                try:
                    tokens = ["push", *shlex.split(tokens[1])]
                except ValueError:
                    continue
            name = tokens[1] if tokens[0] == "push" and len(tokens) > 1 else tokens[0]
            args = tokens[2:] if tokens[0] == "push" else tokens[1:]
            if name not in {"dev", "proto", "port", "topology", "server", "server-ipv6", "route", "route-ipv6", "route-gateway", "redirect-gateway", "client-to-client", "config"}:
                continue
            if name in ("server", "route") and len(args) >= 2:
                try:
                    args = [str(ipaddress.ip_network((args[0], args[1]), strict=False))]
                except ValueError:
                    args = ["invalid-network"]
            elif name == "port":
                args = [int(args[0])] if args and args[0].isdigit() and 0 < int(args[0]) <= 65535 else ["invalid-port"]
            elif name in ("route-gateway", "config"):
                args = ["arguments-withheld"]
            output.append({"directive": ("push " if tokens[0] == "push" else "") + name,
                           "arguments": self.sanitize(args)})
        return output

    @staticmethod
    def persistence_structure(text):
        # Hints only. Includes and unit overrides can change the effective boot ruleset.
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        return {"forward_hook_text_present": bool(re.search(r"\bhook\s+forward\b", code)),
                "snat_or_masquerade_text_present": bool(re.search(r"\b(?:snat|masquerade)\b", code)),
                "secforge_marker_text_present": "secforge-vpn-authz" in code,
                "include_directives_present": bool(re.search(r"\binclude\s", code))}

    def persistence_files(self):
        result = {}
        try:
            result["nft_config_on_disk"] = {"readable": True, **self.persistence_structure(Path("/etc/nftables.conf").read_text())}
        except OSError:
            result["nft_config_on_disk"] = {"readable": False}
        try:
            tables = Path("/proc/net/ip_tables_names").read_text().splitlines()
            result["legacy_kernel_tables"] = {"readable": True, "names": [t for t in tables if t in {"nat", "filter", "mangle", "raw", "security"}]}
        except OSError:
            result["legacy_kernel_tables"] = {"readable": False}
        return result

    def nft_report(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("nftables"), list):
            return {"available": False}
        objects = data["nftables"]
        chains = {(c["family"], c["table"], c["name"]): c for o in objects if isinstance(o, dict) and (c := o.get("chain"))}
        rules = [o["rule"] for o in objects if isinstance(o, dict) and "rule" in o]
        selected = {key for key, chain in chains.items() if chain.get("hook") in ("forward", "prerouting", "postrouting") or chain.get("type") == "nat"}
        def keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    yield key
                    yield from keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from keys(nested)
        def jumps(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in ("jump", "goto") and isinstance(nested, dict) and isinstance(nested.get("target"), str):
                        yield nested["target"]
                    yield from jumps(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from jumps(nested)
        while True:
            found = {(r["family"], r["table"], jump) for r in rules
                     if (r["family"], r["table"], r["chain"]) in selected
                     for jump in jumps(r.get("expr", []))}
            expanded = selected | (found & chains.keys())
            if expanded == selected:
                break
            selected = expanded
        marked = [r for r in rules if r.get("comment") == "secforge-vpn-authz"]
        return {"available": True, "expected_secforge_forward_chain_present": ("inet", "filter", "forward") in chains,
            "secforge_marked_rule_count": len(marked),
            "marked_rules_in_expected_forward_chain": sum((r["family"], r["table"], r["chain"]) == ("inet", "filter", "forward") for r in marked),
            "snat_or_masquerade_present": any(k in ("snat", "masquerade") for r in rules for k in keys(r.get("expr", []))),
            "chains": [{"label": self.label("chain", key), "name_label": self.sanitize(c.get("name")),
                "table_label": self.label("table", (c["family"], c["table"])), "family": self.sanitize(c.get("family")),
                "priority": self.sanitize(c.get("prio")),
                "hook": self.sanitize(c.get("hook")), "type": self.sanitize(c.get("type")), "policy": self.sanitize(c.get("policy")),
                "rules": [{"is_secforge_authorization": r.get("comment") == "secforge-vpn-authz", "expressions": self.sanitize(r.get("expr", []))}
                          for r in rules if (r["family"], r["table"], r["chain"]) == key]}
                       for key, c in chains.items() if key in selected],
            "sets": [{"label": self.label("symbol", s.get("name")), "type": self.sanitize(s.get("type")), "elements": self.sanitize(s.get("elem", []))}
                     for o in objects if isinstance(o, dict) and (s := o.get("set"))],
        }

    def collect(self):
        services = self.run("services")
        units = [line.split()[0] for line in (services or "").splitlines() if line.split() and UNIT.fullmatch(line.split()[0])]
        service_rows = []
        for unit in units[:5]:
            text = self.run("unit", unit)
            props = dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)
            config_paths = re.findall(r"--config(?:=|\s+)([^\s;]+)", props.get("ExecStart", ""))
            paths = [Path(p.strip('\"')) for p in config_paths]
            paths = [p if p.is_absolute() else Path(props.get("WorkingDirectory", "")) / p for p in paths]
            service_rows.append({"label": self.label("service", unit),
                "active_state": self.sanitize(props.get("ActiveState")), "sub_state": self.sanitize(props.get("SubState")),
                "unit_file_state": self.sanitize(props.get("UnitFileState")),
                "configs": [self.config(p) for p in paths],
                "effective_config_identified": bool(config_paths),
            })
        forwarding = self.run("forwarding")
        nft = self.nft_report(self.json_command("nft"))
        routes = self.json_command("routes")
        persistence = self.run("nft_service")
        persistence_props = dict(line.split("=", 1) for line in (persistence or "").splitlines() if "=" in line)
        legacy = {}
        for kind in ("nat", "filter"):
            text = self.run("legacy_" + kind)
            legacy[kind] = {"available": text is not None,
                "rules": [self.sanitize(shlex.split(line)) for line in (text or "").splitlines() if line.startswith(("-A ", ":FORWARD ", ":POSTROUTING ", ":PREROUTING "))],
                "snat_or_masquerade_present": bool(re.search(r"-j\s+(?:SNAT|MASQUERADE)\b", text or "")) if text is not None else None}
        return {"schema_version": 1, "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "openvpn_services": service_rows, "services_truncated": len(units) > 5,
            "ipv4_forwarding": forwarding.strip() == "1" if forwarding is not None and forwarding.strip() in ("0", "1") else None,
            "nftables": nft, "legacy_iptables": legacy,
            "ipv4_routes": [{k: self.sanitize(r[k]) for k in ("dst", "gateway", "dev", "protocol", "scope", "type", "table") if k in r}
                            for r in routes] if isinstance(routes, list) else None,
            "nftables_service": {k: self.sanitize(persistence_props.get(k)) for k in ("ActiveState", "UnitFileState")},
            **self.persistence_files(),
            "errors": self.errors,
            "limitations": [
                "Read-only configuration snapshot, not packet observation or proof of authorized/unauthorized connectivity.",
                "Service ExecStart config files are disk configuration; reload status and included configs are not established.",
                "Unit enablement is evidence only; boot ruleset equivalence and backend reconstruction must be confirmed by the operator.",
                "Unrecognized expression fields are flagged; do not infer absence of restrictions or NAT from incomplete data.",
                "Host addresses and names are anonymized; VPN/challenge host roles use the expected networks, not verified user identities.",
            ]}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid arguments; use --help. Nothing collected.\n")


def main(argv=None):
    parser = SafeParser(description="Read-only snapshot on the existing VPN gateway")
    parser.add_argument("--allow-read-only-gateway", action="store_true", required=True)
    parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Approved read-only elevated access is required; nothing collected.", file=sys.stderr)
        return 1
    try:
        report = Collector().collect()
        print(json.dumps(report, indent=2))
    except Exception:
        print("Gateway snapshot failed; raw diagnostics withheld.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
