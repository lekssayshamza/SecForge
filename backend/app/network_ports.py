"""Bounded room service policy shared by the API, Terraform and reconciliation."""
from typing import Annotated, Literal
from ipaddress import ip_network

from pydantic import BaseModel, Field, TypeAdapter, model_validator


class PortRule(BaseModel):
    protocol: Literal['tcp', 'udp']
    from_port: int = Field(strict=True, ge=1, le=65535)
    to_port: int = Field(strict=True, ge=1, le=65535)
    model_config = {'extra': 'forbid'}

    @model_validator(mode='after')
    def ordered(self):
        if self.to_port < self.from_port:
            raise ValueError('Range end must be at least the start port')
        return self


PortRules = Annotated[list[PortRule], Field(max_length=20)]
DEFAULT_PORTS = [dict(protocol='tcp', from_port=p, to_port=p) for p in (22, 80)]
RULE_DESCRIPTION = 'SecForge room ports v1'


def normalize_ports(value):
    rules = TypeAdapter(PortRules).validate_python(value)
    merged = []
    for rule in sorted(rules, key=lambda r: (r.protocol, r.from_port, r.to_port)):
        if merged and merged[-1]['protocol'] == rule.protocol and rule.from_port <= merged[-1]['to_port'] + 1:
            merged[-1]['to_port'] = max(merged[-1]['to_port'], rule.to_port)
        else:
            merged.append(rule.model_dump())
    if sum(r['to_port'] - r['from_port'] + 1 for r in merged) > 4096:
        raise ValueError('Expose at most 4096 TCP/UDP ports per room')
    return merged


def vpn_source(value):
    network = ip_network(value, strict=True)
    if (network.version != 4 or network.prefixlen < 16
            or not any(network.subnet_of(ip_network(block)) for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))):
        raise ValueError('Room ports require a private IPv4 VPN CIDR of /16 or narrower')
    return str(network)


def room_ports(db, resource, kind):
    from app.models import Room, RoomDraft
    from sqlalchemy import select
    # Transient environments use legacy defaults. A persisted launch must load
    # its room through the attached session rather than silently losing policy.
    if db is None:
        from sqlalchemy import inspect
        if not inspect(resource).transient:
            raise ValueError('A database session is required for room network policy')
        return normalize_ports(DEFAULT_PORTS)
    room = db.scalar(select(Room).where(Room.id == resource.room_id).execution_options(populate_existing=True))
    if room is None:
        raise ValueError('Room network policy unavailable')
    ports = room.exposed_ports
    if kind == 'builder' or resource.learner_id == room.creator_id:
        draft = db.scalar(select(RoomDraft).where(RoomDraft.room_id == room.id).execution_options(populate_existing=True))
        if draft:
            ports = draft.document['metadata'].get('exposed_ports', ports)
    return normalize_ports(ports)
