"""Reconcile the cluster security group + rules, the port of security_group.tf.

Rules: ICMP, one rule per host CIDR of every named `security:` entry (on that
entry's port), tcp/80 and tcp/443 open to all unless an `http` or `https` entry
restricts them, and intra-SG allow-all tcp+udp. Editing an allowlist in
cluster.yaml converges here.

This is the one place true diffing matters: we compute the desired ingress rule
set as comparable tuples, then add the missing ones and delete the extra ones.
Only INGRESS rules are touched -- Neutron's default egress allow-all rules are
left alone.
"""

from __future__ import annotations

from typing import Any

from openstack.connection import Connection

from .. import naming
from ..config import Config
from ..output import action, dry_run, info
from .session import Inventory
from .tags import create_tagged

# a normalized, hashable rule: (protocol, pmin, pmax, remote_ip, remote_group_ref)
# remote_group_ref is the sentinel "@self" for intra-SG rules (resolved to the
# sg id at create time), else None.
SELF = "@self"


def _desired_rules(cfg: Config) -> dict[tuple, str]:
    """desired rule tuple -> human description."""
    rules: dict[tuple, str] = {}
    rules[("icmp", None, None, None, None)] = "icmp"
    for port in cfg.open_ports():
        rules[("tcp", port, port, None, None)] = f"tcp/{port} open"
    for rule in cfg.security.values():
        for name, cidr in rule.hosts.items():
            rules[("tcp", rule.port, rule.port, cidr, None)] = f"{rule.name} from {name}"
    rules[("tcp", None, None, None, SELF)] = "intra-sg tcp"
    rules[("udp", None, None, None, SELF)] = "intra-sg udp"
    return rules


def _rule_key(r: Any, sg_id: str) -> tuple | None:
    """Normalize an existing Neutron rule to a desired-comparable tuple, or None
    if it's not an ingress IPv4 rule we manage."""
    ether = getattr(r, "ether_type", None) or getattr(r, "ethertype", None)
    if r.direction != "ingress" or ether != "IPv4":
        return None
    remote_group = SELF if r.remote_group_id == sg_id else None
    # normalize "0.0.0.0/0" to None so clouds that materialize the default
    # prefix don't flap add/delete against clouds that store null
    remote_ip = r.remote_ip_prefix
    if remote_ip == "0.0.0.0/0":
        remote_ip = None
    return (
        r.protocol,
        r.port_range_min,
        r.port_range_max,
        remote_ip,
        remote_group,
    )


def reconcile(conn: Connection, cfg: Config, inv: Inventory) -> Any:
    cluster = cfg.name
    name = naming.secgroup_name(cluster)
    tags = naming.base_tags(cluster)

    sg = inv.get("security_groups", name)
    if sg is None:
        action(f"create security group {name}")
        if dry_run():
            return None
        sg = create_tagged(
            conn.network,
            "security_group",
            tags,
            name=name,
            description=f"{cluster} kubernetes cluster security group",
        )
        inv.put("security_groups", sg)
    else:
        info(f"security group {name} exists")

    if sg is None:
        return None

    desired = _desired_rules(cfg)
    existing = list(conn.network.security_group_rules(security_group_id=sg.id))
    existing_keys = {}
    for r in existing:
        key = _rule_key(r, sg.id)
        if key is not None:
            existing_keys[key] = r

    # add missing
    for key, desc in desired.items():
        if key in existing_keys:
            continue
        _create_rule(conn, sg, key, desc)

    # delete extra ingress rules we manage but no longer want
    for key, r in existing_keys.items():
        if key not in desired:
            action(f"delete security group rule {key}")
            if not dry_run():
                conn.network.delete_security_group_rule(r.id)

    return sg


def _create_rule(conn, sg, key, desc) -> None:
    proto, pmin, pmax, remote_ip, remote_group = key
    action(f"create security group rule: {desc}")
    if dry_run():
        return
    kwargs = dict(
        security_group_id=sg.id,
        direction="ingress",
        ethertype="IPv4",
        protocol=proto,
        description=desc,
    )
    if pmin is not None:
        kwargs["port_range_min"] = pmin
        kwargs["port_range_max"] = pmax
    if remote_ip is not None:
        kwargs["remote_ip_prefix"] = remote_ip
    if remote_group == SELF:
        kwargs["remote_group_id"] = sg.id
    conn.network.create_security_group_rule(**kwargs)
