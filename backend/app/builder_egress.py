"""Builder-only internet policy, composed into the existing atomic VPN sync.

DNS uses the VPC resolver; only public IPv4 HTTP/HTTPS is forwarded/NATed.
The earlier-priority guard also denies stale established connections after
an address is removed. It never matches VPN tunnel traffic or private traffic.
"""

import ipaddress
import re

from sqlalchemy import select

from app.config import settings
from app.lab_time import utc_now
from app.models import BuilderEnvironment, User

TABLE = "secforge-builder-egress"
COMMENT = "secforge-builder-egress"
# Disjoint non-public IPv4 ranges. In particular, AWS metadata, VPC and VPN
# destinations never match the internet policy. No IPv6 forwarding is added.
NON_PUBLIC = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


def desired_builder_ips(db):
    """Only registered, live creator builders qualify; never learner labs."""
    from app.vpn_authz import validate_lab_ip

    return {
        validate_lab_ip(ip)
        for ip in db.scalars(
            select(BuilderEnvironment.private_ip)
            .join(User, User.id == BuilderEnvironment.creator_id)
            .where(
                User.role.in_(("creator", "admin")),
                User.suspended.is_(False),
                BuilderEnvironment.status == "running",
                BuilderEnvironment.instance_id.is_not(None),
                BuilderEnvironment.instance_id != "",
                BuilderEnvironment.private_ip.is_not(None),
                BuilderEnvironment.expires_at > utc_now(),
            )
        )
    }


def render_policy(builder_ips):
    """Return owned table and same-chain forward accept, fully validated.

    Empty addresses produce a persistent fail-closed policy. On gateway boot
    that empty policy is loaded; normal startup/reconciliation restores live
    addresses. No alternate authorization worker is introduced.
    """
    from app.vpn_authz import VpnAuthorizationError, validate_lab_ip

    interface = settings.builder_internet_gateway_interface
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}", interface or "") or interface.startswith(("tun", "tap", "lo")):
        raise VpnAuthorizationError("Builder internet interface is invalid")
    network = ipaddress.ip_network(settings.lab_private_cidr, strict=True)
    private_ranges = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if network.version != 4 or not any(network.subnet_of(n) for n in private_ranges):
        raise VpnAuthorizationError("Builder internet requires a private IPv4 subnet")
    ips = sorted({validate_lab_ip(ip) for ip in builder_ips}, key=ipaddress.ip_address)
    public = "ip daddr != { " + ", ".join(NON_PUBLIC) + " }"
    path = f'iifname "{interface}" oifname "{interface}" ip saddr {network} {public}'
    sources = "ip saddr { " + ", ".join(ips) + " }"
    ports = "tcp dport { 80, 443 }"
    lines = [f"destroy table ip {TABLE}", f"table ip {TABLE} {{",
             " chain guard { type filter hook forward priority -10; policy accept;"]
    if ips:
        lines.append(f"  {path} {sources} {ports} counter accept")
    lines += [f"  {path} counter drop", " }",
              " chain postrouting { type nat hook postrouting priority srcnat; policy accept;"]
    if ips:
        lines.append(f"  {path} {sources} {ports} counter masquerade")
    lines += [" }", "}"]
    # iifname is not available at the postrouting hook. The guard already
    # verifies ingress; source and destination/interface restrictions remain.
    lines = [line.replace(f'iifname "{interface}" ', "") if "masquerade" in line else line for line in lines]
    accept = (f'add rule inet filter forward {path} {sources} {ports} '
              f'counter accept comment "{COMMENT}"') if ips else ""
    return "\n".join(lines) + "\n", accept
