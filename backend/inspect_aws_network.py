"""Opt-in EC2 network inventory. AWS CLI only; no application imports or raw dumps."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys


REGION = "eu-north-1"
OPERATIONS = {
    "describe-instances": "Reservations",
    "describe-security-groups": "SecurityGroups",
    "describe-vpcs": "Vpcs",
    "describe-subnets": "Subnets",
    "describe-route-tables": "RouteTables",
    "describe-network-interfaces": "NetworkInterfaces",
}
CALL_TIMEOUT_SECONDS = 35  # Six sequential calls: at most 210 seconds of CLI execution.
DEFAULT_OUTPUT = "secforge-aws-network-report.json"


class InspectionError(Exception):
    """Only fixed, non-sensitive messages are attached to this exception."""


def read_aws(operation):
    if operation not in OPERATIONS:
        raise InspectionError("Operation is not in the read-only allowlist.")
    command = [
        "aws", "ec2", operation, "--region", REGION,
        "--endpoint-url", f"https://ec2.{REGION}.amazonaws.com",
        "--output", "json", "--no-cli-pager",
        "--cli-connect-timeout", "5", "--cli-read-timeout", "10",
    ]
    environment = dict(os.environ, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off",
                       AWS_EC2_METADATA_DISABLED="true", AWS_MAX_ATTEMPTS="1",
                       AWS_RETRY_MODE="standard")
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=CALL_TIMEOUT_SECONDS, env=environment,
                                shell=False)
    except subprocess.TimeoutExpired:
        raise InspectionError("AWS inspection timed out; no report written.") from None
    except OSError:
        raise InspectionError("AWS CLI could not be executed; no report written.") from None
    if result.returncode:
        error = result.stderr.lower()
        if "unable to locate credentials" in error or "expiredtoken" in error:
            reason = "AWS authentication unavailable or expired."
        elif "accessdenied" in error or "unauthorizedoperation" in error:
            reason = "AWS describe permission denied."
        elif any(term in error for term in ("endpoint", "name resolution", "proxy", "network is unreachable")):
            reason = "AWS endpoint unavailable."
        else:
            reason = "AWS CLI request failed; raw diagnostics withheld."
        raise InspectionError(f"{reason} No report written.")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise InspectionError("AWS returned invalid JSON; no report written.") from None
    if not isinstance(data, dict) or not isinstance(data.get(OPERATIONS[operation]), list):
        raise InspectionError("AWS response shape was unexpected; no report written.")
    return data


def choice(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


def boolean(value):
    return value if isinstance(value, bool) else None


def role_hint(instance):
    # Tags are hints only. Never copy names, descriptions, tag values or owners out.
    tags = {t.get("Key"): str(t.get("Value", "")).lower()
            for t in instance.get("Tags", [])}
    if (tags.get("ResourceType") == "builder" or tags.get("Environment") == "builder"
            or tags.get("Name", "").startswith("builder-")):
        return "builder"
    if (tags.get("ResourceType") == "lab" or tags.get("Environment") == "learner-lab"
            or tags.get("Name", "").startswith("lab-")):
        return "challenge-lab"
    hints = " ".join(tags.get(key, "") for key in ("Name", "Role", "Service", "ResourceType", "Environment"))
    if any(word in hints for word in ("openvpn", "vpn", "gateway")):
        return "gateway-candidate"
    return "unclassified"


class Labels:
    def __init__(self):
        self.labels = {}
        self.counts = Counter()

    def prime(self, kind, values):
        for value in sorted({v for v in values if isinstance(v, str) and v}):
            self.get(kind, value)

    def get(self, kind, value):
        if not isinstance(value, str) or not value:
            return None
        key = (kind, value)
        if key not in self.labels:
            self.counts[kind] += 1
            self.labels[key] = f"{kind}-{self.counts[kind]}"
        return self.labels[key]


def sanitize_inventory(inventory):
    """Construct an allowlisted report, never recursively copy arbitrary AWS data."""
    instances = [i for r in inventory["describe-instances"]["Reservations"]
                 for i in r.get("Instances", [])]
    groups = inventory["describe-security-groups"]["SecurityGroups"]
    vpcs = inventory["describe-vpcs"]["Vpcs"]
    subnets = inventory["describe-subnets"]["Subnets"]
    tables = inventory["describe-route-tables"]["RouteTables"]
    interfaces = inventory["describe-network-interfaces"]["NetworkInterfaces"]
    labels = Labels()
    for kind, rows, key in (("instance", instances, "InstanceId"),
                            ("group", groups, "GroupId"), ("vpc", vpcs, "VpcId"),
                            ("subnet", subnets, "SubnetId"),
                            ("route-table", tables, "RouteTableId"),
                            ("interface", interfaces, "NetworkInterfaceId")):
        labels.prime(kind, [row.get(key) for row in rows])
    owners = {i.get("InstanceId"): role_hint(i) for i in instances}
    address_owners = {}
    for interface in interfaces:
        owner = interface.get("Attachment", {}).get("InstanceId")
        label = labels.get("instance", owner) or labels.get("interface", interface.get("NetworkInterfaceId"))
        for item in interface.get("PrivateIpAddresses", []):
            address_owners[item.get("PrivateIpAddress")] = label
            address_owners[item.get("Association", {}).get("PublicIp")] = label
        address_owners[interface.get("PrivateIpAddress")] = label
        address_owners[interface.get("Association", {}).get("PublicIp")] = label
        for item in interface.get("Ipv6Addresses", []):
            address_owners[item.get("Ipv6Address")] = label

    def network(value):
        try:
            net = ipaddress.ip_network(value, strict=False)
        except (ValueError, TypeError):
            return "invalid-or-unavailable-network"
        if net.prefixlen == net.max_prefixlen:
            owner = address_owners.get(str(net.network_address))
            label = f"host-of-{owner}" if owner else labels.get("host-network", str(net))
            return f"{label} (/{net.prefixlen})"
        return str(net)  # Network CIDRs are intentionally retained; host IPs are not.

    def memberships(row):
        return sorted({labels.get("group", g.get("GroupId")) for g in row.get("Groups", row.get("SecurityGroups", []))
                       if g.get("GroupId")})

    def protocol(value):
        known = {"-1": "all", "6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6"}
        if str(value) in known:
            return known[str(value)]
        if value in ("tcp", "udp", "icmp", "icmpv6"):
            return value
        try:
            number = int(value)
            return f"ip-protocol-{number}" if 0 <= number <= 255 else "unknown"
        except (ValueError, TypeError):
            return "unknown"

    def permissions(rows):
        output = []
        for rule in rows:
            peers = []
            for key, field in (("IpRanges", "CidrIp"), ("Ipv6Ranges", "CidrIpv6")):
                peers.extend({"network": network(r.get(field))} for r in rule.get(key, []))
            for peer in rule.get("UserIdGroupPairs", []):
                peers.append({"group": labels.get("group", peer.get("GroupId")),
                              "vpc": labels.get("vpc", peer.get("VpcId")),
                              "group_in_inventory": any(g.get("GroupId") == peer.get("GroupId") for g in groups)})
            peers.extend({"prefix_list": labels.get("prefix-list", p.get("PrefixListId"))}
                         for p in rule.get("PrefixListIds", []))
            output.append({"protocol": protocol(rule.get("IpProtocol")),
                           "from_port_or_icmp_type": rule.get("FromPort") if type(rule.get("FromPort")) is int and -1 <= rule["FromPort"] <= 65535 else None,
                           "to_port_or_icmp_code": rule.get("ToPort") if type(rule.get("ToPort")) is int and -1 <= rule["ToPort"] <= 65535 else None,
                           "peers": peers})
        return output

    def target(route):
        for key, kind in (("InstanceId", "instance"), ("NetworkInterfaceId", "interface"),
                          ("NatGatewayId", "nat-gateway"), ("TransitGatewayId", "transit-gateway"),
                          ("VpcPeeringConnectionId", "peering"),
                          ("EgressOnlyInternetGatewayId", "egress-only-gateway"),
                          ("LocalGatewayId", "local-gateway"), ("CarrierGatewayId", "carrier-gateway"),
                          ("CoreNetworkArn", "core-network"), ("OdbNetworkArn", "odb-network")):
            if route.get(key):
                result = {"resource": labels.get(kind, route[key])}
                owner = route[key] if key == "InstanceId" else next((
                    e.get("Attachment", {}).get("InstanceId") for e in interfaces
                    if key == "NetworkInterfaceId" and e.get("NetworkInterfaceId") == route[key]), None)
                if owner:
                    result.update(instance=labels.get("instance", owner), role_hint=owners.get(owner, "unclassified"))
                return result
        gateway = route.get("GatewayId")
        if gateway == "local":
            return {"resource": "vpc-local"}
        if gateway:
            kind = "internet-gateway" if gateway.startswith("igw-") else "virtual-gateway" if gateway.startswith("vgw-") else "vpc-endpoint" if gateway.startswith("vpce-") else "gateway"
            return {"resource": labels.get(kind, gateway)}
        return {"resource": "unknown"}

    def subnet_routes(subnet):
        explicit, main = [], []
        for table in tables:
            if table.get("VpcId") != subnet.get("VpcId"):
                continue
            for association in table.get("Associations", []):
                state = association.get("AssociationState", {}).get("State", "associated")
                if state != "associated":
                    continue
                label = labels.get("route-table", table.get("RouteTableId"))
                if association.get("SubnetId") == subnet.get("SubnetId"):
                    explicit.append(label)
                if association.get("Main") is True:
                    main.append(label)
        return {"route_tables": sorted(set(explicit or main)),
                "association": "explicit" if explicit else "inherited-main" if main else "unknown"}

    instance_rows = []
    for i in sorted(instances, key=lambda r: r.get("InstanceId", "")):
        state = choice(i.get("State", {}).get("Name"), {"pending", "running", "stopping", "stopped", "shutting-down", "terminated"})
        role = role_hint(i)
        instance_rows.append({"label": labels.get("instance", i.get("InstanceId")),
            "role_hint": role, "state": state, "vpc": labels.get("vpc", i.get("VpcId")),
            "subnet": labels.get("subnet", i.get("SubnetId")), "groups": memberships(i),
            "source_dest_check": boolean(i.get("SourceDestCheck")),
            "has_public_ip": bool(i.get("PublicIpAddress")),
            "interfaces": sorted(labels.get("interface", n["NetworkInterfaceId"]) for n in i.get("NetworkInterfaces", []) if n.get("NetworkInterfaceId")),
            "running_builder_or_lab_charge_candidate": role in ("builder", "challenge-lab") and state in ("running", "pending", "stopping"),
        })
    interface_rows = [{"label": labels.get("interface", e.get("NetworkInterfaceId")),
        "instance": labels.get("instance", e.get("Attachment", {}).get("InstanceId")),
        "role_hint": owners.get(e.get("Attachment", {}).get("InstanceId"), "unclassified"),
        "vpc": labels.get("vpc", e.get("VpcId")), "subnet": labels.get("subnet", e.get("SubnetId")),
        "groups": memberships(e), "status": choice(e.get("Status"), {"available", "in-use"}),
        "interface_type": choice(e.get("InterfaceType"), {"interface", "nat_gateway", "vpc_endpoint",
            "load_balancer", "network_load_balancer", "gateway_load_balancer", "gateway_load_balancer_endpoint",
            "transit_gateway", "lambda", "efa", "trunk", "branch"}),
        "source_dest_check": boolean(e.get("SourceDestCheck")),
        "has_public_ip": bool(e.get("Association", {}).get("PublicIp")) or any(bool(p.get("Association", {}).get("PublicIp")) for p in e.get("PrivateIpAddresses", [])),
    } for e in sorted(interfaces, key=lambda r: r.get("NetworkInterfaceId", ""))]
    return {
        "schema_version": 1, "region": REGION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "All visible EC2 networking resources in this region; roles inferred from tags, not verified.",
        "instances": instance_rows, "network_interfaces": interface_rows,
        "security_groups": [{"label": labels.get("group", g.get("GroupId")),
            "vpc": labels.get("vpc", g.get("VpcId")),
            "ingress": permissions(g.get("IpPermissions", [])),
            "egress": permissions(g.get("IpPermissionsEgress", []))}
            for g in sorted(groups, key=lambda r: r.get("GroupId", ""))],
        "vpcs": [{"label": labels.get("vpc", v.get("VpcId")),
            "ipv4_cidrs": [network(a.get("CidrBlock")) for a in v.get("CidrBlockAssociationSet", [])]
                or ([network(v["CidrBlock"])] if v.get("CidrBlock") else []),
            "ipv6_cidrs": [network(a.get("Ipv6CidrBlock")) for a in v.get("Ipv6CidrBlockAssociationSet", [])]}
            for v in sorted(vpcs, key=lambda r: r.get("VpcId", ""))],
        "subnets": [{"label": labels.get("subnet", s.get("SubnetId")),
            "vpc": labels.get("vpc", s.get("VpcId")), "ipv4_cidr": network(s.get("CidrBlock")),
            "ipv6_cidrs": [network(a.get("Ipv6CidrBlock")) for a in s.get("Ipv6CidrBlockAssociationSet", [])],
            "map_public_ip_on_launch": boolean(s.get("MapPublicIpOnLaunch")), **subnet_routes(s)}
            for s in sorted(subnets, key=lambda r: r.get("SubnetId", ""))],
        "route_tables": [{"label": labels.get("route-table", t.get("RouteTableId")),
            "vpc": labels.get("vpc", t.get("VpcId")),
            "routes": [{"destination": network(r.get("DestinationCidrBlock", r.get("DestinationIpv6CidrBlock")))
                        if not r.get("DestinationPrefixListId") else labels.get("prefix-list", r["DestinationPrefixListId"]),
                        "state": choice(r.get("State"), {"active", "blackhole"}), "target": target(r)}
                       for r in t.get("Routes", [])]}
            for t in sorted(tables, key=lambda r: r.get("RouteTableId", ""))],
        "limitations": [
            "AWS CLI inventory cannot establish guest OpenVPN routes, NAT/MASQUERADE, IP forwarding, nftables isolation or persistence.",
            "Security-group references and route targets are configuration evidence, not proof of working packet flow.",
            "Prefix-list contents, network ACLs, other regions and non-EC2 resource inventories are not inspected.",
            "Running/pending instances may incur charges; stopped instances may retain chargeable EBS. This is not a billing or orphan determination.",
            "Host /32 and /128 CIDRs are anonymized; broader network CIDRs, protocols and ports are retained intentionally.",
        ],
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid arguments; use --help. No AWS inspection started.\n")


def main(argv=None):
    parser = SafeParser(description="Read-only, sanitized EC2 network inventory in eu-north-1")
    parser.add_argument("--allow-read-only-aws", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    if os.path.lexists(args.output):
        print("Output already exists; choose a new filename. Nothing overwritten.", file=sys.stderr)
        return 1
    try:
        inventory = {operation: read_aws(operation) for operation in OPERATIONS}
        report = sanitize_inventory(inventory)
        text = json.dumps(report, indent=2) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(args.output, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except InspectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Inspection or report writing failed; raw diagnostics withheld. Any partial output is sanitized.", file=sys.stderr)
        return 1
    print("Sanitized report written. No AWS resources were changed.")
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
