"""Reconcile the cluster security group + rules, the port of security_group.tf.

Rules: ICMP, one rule per host CIDR of every named `security:` entry (on that
entry's port), tcp/80 and tcp/443 open to all unless an `http` or `https` entry
restricts them, intra-SG allow-all tcp+udp, and -- when a `metal` group sits on
another L2 -- tcp+udp plus KubeSpan's UDP/51820 from that group's CIDR. Editing
an allowlist in cluster.yaml converges here.

Egress is the metadata-service block: Neutron seeds every new security group
with two allow-all egress rules, which are removed and replaced by rules
allowing every destination except the Nova metadata address (IPv4 and the IPv6
link-local form Neutron also answers on). That cuts node and masqueraded pod
traffic to it under any CNI, in every namespace.

This is the one place true diffing matters: we compute the desired ingress and
egress rule sets as comparable tuples, then add the missing ones and delete the
extra ones.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from openstack.connection import Connection

from .. import naming
from ..config import KUBESPAN_PORT, Config
from ..output import action, dry_run, info
from .session import Inventory
from .tags import create_tagged

# a normalized, hashable rule: (protocol, pmin, pmax, remote_ip, remote_group_ref)
# remote_group_ref is the sentinel "@self" for intra-SG rules (resolved to the
# sg id at create time), None for open-to-all, or a foreign security-group id.
SELF = "@self"

# The metadata address Nova serves user_data at, and the IPv6 link-local form
# Neutron also answers it on. Both are denied by egress rules: pod traffic is
# masqueraded behind the node, so the security group cuts off every namespace
# under any CNI.
METADATA_V4 = "169.254.169.254/32"
METADATA_V6 = "fe80::/10"


def _normalize_cidr(cidr: str | None) -> str | None:
    """Normalize the wildcard prefix to None so clouds that materialize the
    default prefix don't flap add/delete against clouds that store null."""
    return None if cidr == "0.0.0.0/0" else cidr


def _complement(cidr: str) -> list[str]:
    """The CIDR set covering the whole address space minus `cidr`.

    Each block keeps `cidr`'s first i prefix bits, flips bit i and clears
    everything below it, so a /32 hole yields 32 blocks and a /10 hole 10.
    """
    net = ipaddress.ip_network(cidr)
    addr = int(net.network_address)
    # (int, prefix) tuples infer IPv4 when they fit, so pick the class directly
    cls = ipaddress.IPv4Network if net.version == 4 else ipaddress.IPv6Network
    blocks = []
    for i in range(net.prefixlen):
        bit = 1 << (net.max_prefixlen - 1 - i)
        flipped = (addr ^ bit) & ~(bit - 1)
        blocks.append(str(cls((flipped, i + 1))))
    return blocks


def _desired_egress_rules() -> dict[tuple, str]:
    """desired egress rule tuple -> human description.

    All-protocol rules for every CIDR around the metadata address, so the two
    default allow-all egress rules can go away without opening anything else.
    """
    rules: dict[tuple, str] = {}
    for cidr in _complement(METADATA_V4):
        rules[("IPv4", None, None, None, cidr, None)] = f"egress allow {cidr}"
    for cidr in _complement(METADATA_V6):
        rules[("IPv6", None, None, None, cidr, None)] = f"egress allow {cidr}"
    return rules


def _desired_rules(cfg: Config) -> dict[tuple, str]:
    """desired rule tuple -> human description."""
    rules: dict[tuple, str] = {}
    rules[("icmp", None, None, None, None)] = "icmp"
    for port in cfg.open_ports():
        rules[("tcp", port, port, None, None)] = f"tcp/{port} open"
    for rule in cfg.security.values():
        for name, cidr in rule.hosts.items():
            rules[("tcp", rule.port, rule.port, _normalize_cidr(cidr), None)] = (
                f"{rule.name} from {name}"
            )
    rules[("tcp", None, None, None, SELF)] = "intra-sg tcp"
    rules[("udp", None, None, None, SELF)] = "intra-sg udp"
    # the members sit inside the SG (SELF); a metal group on another L2 is not,
    # so its nodes are admitted by CIDR -- apid, kubelet and etcd over tcp+udp,
    # their WireGuard handshakes on the explicit KubeSpan port
    peers = [
        subnet for subnet in cfg.intra_cluster_cidrs()
        if subnet != cfg.network.cluster.cidr
    ]
    for subnet in peers:
        rules[("tcp", None, None, subnet, None)] = "intra-cluster tcp"
        rules[("udp", None, None, subnet, None)] = "intra-cluster udp"
        rules[("udp", KUBESPAN_PORT, KUBESPAN_PORT, subnet, None)] = "kubespan udp"
    return rules


def _rule_key(r: Any, sg_id: str) -> tuple | None:
    """Normalize an existing Neutron rule to a desired-comparable tuple, or None
    if it's not an ingress IPv4 rule we manage."""
    ether = getattr(r, "ether_type", None) or getattr(r, "ethertype", None)
    if r.direction != "ingress" or ether != "IPv4":
        return None
    # A remote group that is not this SG's own id must stay distinct: it is not
    # open-to-all (None) nor intra-SG (SELF), so it cannot mask an open rule.
    remote_group = SELF if r.remote_group_id == sg_id else r.remote_group_id
    remote_ip = _normalize_cidr(r.remote_ip_prefix)
    return (
        r.protocol,
        r.port_range_min,
        r.port_range_max,
        remote_ip,
        remote_group,
    )


def _egress_key(r: Any, sg_id: str) -> tuple | None:
    """Normalize an existing Neutron rule to an egress-comparable tuple, or None
    if it's not an egress rule we manage. Every egress rule is managed: the
    desired set is the exact allowlist, so anything else (Neutron's seeded
    allow-all pair included) is an extra to delete."""
    ether = getattr(r, "ether_type", None) or getattr(r, "ethertype", None)
    if r.direction != "egress" or ether not in ("IPv4", "IPv6"):
        return None
    return (
        ether,
        r.protocol,
        r.port_range_min,
        r.port_range_max,
        _normalize_cidr(r.remote_ip_prefix),
        r.remote_group_id,
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
    desired_egress = _desired_egress_rules()
    existing = list(conn.network.security_group_rules(security_group_id=sg.id))
    ingress_keys: dict[tuple, Any] = {}
    egress_keys: dict[tuple, Any] = {}
    for r in existing:
        key = _rule_key(r, sg.id)
        if key is not None:
            ingress_keys[key] = r
            continue
        key = _egress_key(r, sg.id)
        if key is not None:
            egress_keys[key] = r

    # add missing
    for key, desc in desired.items():
        if key in ingress_keys:
            continue
        _create_rule(conn, sg, key, desc)
    for key, desc in desired_egress.items():
        if key in egress_keys:
            continue
        _create_egress_rule(conn, sg, key, desc)

    # delete extra ingress rules we manage but no longer want
    for key, r in ingress_keys.items():
        if key not in desired:
            action(f"delete security group rule {key}")
            if not dry_run():
                conn.network.delete_security_group_rule(r.id)

    # delete extra egress rules -- Neutron seeds allow-all ones that would
    # otherwise keep the metadata address reachable
    for key, r in egress_keys.items():
        if key not in desired_egress:
            action(f"delete security group rule {key}")
            if not dry_run():
                conn.network.delete_security_group_rule(r.id)

    return sg


def _create_rule(conn, sg, key, desc) -> None:
    proto, pmin, pmax, remote_ip, remote_group = key
    _post_rule(
        conn, sg, direction="ingress", ethertype="IPv4", proto=proto, pmin=pmin,
        pmax=pmax, remote_ip=remote_ip, remote_group=remote_group, desc=desc,
    )


def _create_egress_rule(conn, sg, key, desc) -> None:
    ether, proto, pmin, pmax, remote_ip, _ = key
    _post_rule(
        conn, sg, direction="egress", ethertype=ether, proto=proto, pmin=pmin,
        pmax=pmax, remote_ip=remote_ip, remote_group=None, desc=desc,
    )


def _post_rule(conn, sg, *, direction, ethertype, proto, pmin, pmax, remote_ip,
               remote_group, desc) -> None:
    action(f"create security group rule: {desc}")
    if dry_run():
        return
    kwargs = dict(
        security_group_id=sg.id,
        direction=direction,
        ethertype=ethertype,
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
