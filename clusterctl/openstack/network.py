"""Reconcile the cluster network, the port of network.tf.

Private network + subnet + router (external gateway) + router interface; a
reserved kubeapi port (its fixed ip is the controlplane VIP) with a floating ip;
an identical reserved ingress port + floating ip; and one port per machine whose
allowed_address_pairs let the VIP float onto the right nodes (kubeapi -> control
planes, ingress -> workers).

Every managed resource is found-by-name in the inventory cache first, created +
tagged only if absent, and its reconciled fields (allowed_address_pairs, fip
association) corrected in place. Read-only external-net lookup is a data source,
never tagged or created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openstack import exceptions
from openstack.connection import Connection

from .. import naming
from ..config import Config, Machine
from ..errors import ReconcileError
from ..output import action, dry_run, info
from .session import Inventory


@dataclass
class NetworkRefs:
    kubeapi_fip: str = ""
    kubeapi_vip: str = ""
    ingress_fip: str = ""
    ingress_vip: str = ""
    machine_private_ips: dict[str, str] = field(default_factory=dict)


def reconcile(
    conn: Connection,
    cfg: Config,
    machines: dict[str, Machine],
    inv: Inventory,
    sg: Any,
) -> NetworkRefs:
    cluster = cfg.name
    tags = naming.base_tags(cluster)

    ext = conn.network.find_network(cfg.external_net)
    if ext is None:
        raise ReconcileError(f"external network '{cfg.external_net}' not found")

    network = _ensure_network(conn, cluster, inv, tags)
    subnet = _ensure_subnet(conn, cfg, network, inv, tags)
    router = _ensure_router(conn, cluster, ext, inv, tags)
    _ensure_router_interface(conn, router, subnet)

    refs = NetworkRefs()

    # reserved VIP ports + floating ips (needed BEFORE machine configs, since the
    # fip is a certSAN and the vip is announced by the controlplanes)
    kube_port = _ensure_port(conn, naming.kubeapi_name(cluster), network, inv, tags)
    refs.kubeapi_vip = _fixed_ip(kube_port)
    refs.kubeapi_fip = _ensure_fip(conn, naming.kubeapi_name(cluster), ext, kube_port, inv, tags)

    ing_port = _ensure_port(conn, naming.ingress_name(cluster), network, inv, tags)
    refs.ingress_vip = _fixed_ip(ing_port)
    refs.ingress_fip = _ensure_fip(conn, naming.ingress_name(cluster), ext, ing_port, inv, tags)

    # per-machine ports with role-based allowed_address_pairs
    for host, m in machines.items():
        pair = refs.kubeapi_vip if m.role == "controlplane" else refs.ingress_vip
        port = _ensure_machine_port(conn, cluster, m, network, sg, pair, inv)
        refs.machine_private_ips[host] = _fixed_ip(port)

    return refs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fixed_ip(port: Any) -> str:
    if port is None:
        return ""
    fixed = getattr(port, "fixed_ips", None) or []
    return fixed[0]["ip_address"] if fixed else ""


def _ensure_network(conn, cluster, inv, tags):
    name = naming.network_name(cluster)
    net = inv.get("networks", name)
    if net:
        info(f"network {name} exists")
        return net
    action(f"create network {name}")
    if dry_run():
        return None
    net = conn.network.create_network(name=name, admin_state_up=True)
    conn.network.set_tags(net, tags)
    return inv.put("networks", net)


def _ensure_subnet(conn, cfg, network, inv, tags):
    name = naming.subnet_name(cfg.name)
    sub = inv.get("subnets", name)
    if sub:
        info(f"subnet {name} exists")
        return sub
    action(f"create subnet {name} ({cfg.cidr})")
    if dry_run() or network is None:
        return None
    sub = conn.network.create_subnet(
        name=name,
        network_id=network.id,
        ip_version=4,
        cidr=cfg.cidr,
        dns_nameservers=list(cfg.dns),
    )
    conn.network.set_tags(sub, tags)
    return inv.put("subnets", sub)


def _ensure_router(conn, cluster, ext, inv, tags):
    name = naming.router_name(cluster)
    rtr = inv.get("routers", name)
    if rtr:
        info(f"router {name} exists")
        return rtr
    action(f"create router {name}")
    if dry_run():
        return None
    rtr = conn.network.create_router(
        name=name,
        admin_state_up=True,
        external_gateway_info={"network_id": ext.id},
    )
    conn.network.set_tags(rtr, tags)
    return inv.put("routers", rtr)


def _ensure_router_interface(conn, router, subnet):
    if router is None or subnet is None:
        return
    try:
        conn.network.add_interface_to_router(router, subnet=subnet.id)
    except exceptions.ConflictException:
        # already attached; interface is idempotent
        return
    except exceptions.BadRequestException as e:
        # some Neutron versions report "already attached" as a 400, not a 409
        if "already has a port" in str(e).lower():
            return
        raise
    action(f"attach router {router.name} to subnet")


def _ensure_port(conn, name, network, inv, tags):
    """A reserved port (kubeapi/ingress VIP holder)."""
    port = inv.get("ports", name)
    if port:
        info(f"port {name} exists")
        return port
    action(f"create port {name}")
    if dry_run() or network is None:
        return None
    port = conn.network.create_port(name=name, network_id=network.id)
    conn.network.set_tags(port, tags)
    return inv.put("ports", port)


def _ensure_machine_port(conn, cluster, m: Machine, network, sg, pair_ip, inv):
    name = naming.machine_name(m.name)
    tags = naming.node_tags(cluster, m.role, m.pool)
    desired_pairs = [{"ip_address": pair_ip}] if pair_ip else []
    port = inv.get("ports", name)
    if port:
        _reconcile_allowed_pairs(conn, port, desired_pairs)
        return port
    action(f"create port {name} (allowed_address_pairs={pair_ip or '-'})")
    if dry_run() or network is None:
        return None
    kwargs = dict(name=name, network_id=network.id)
    if sg is not None:
        kwargs["security_group_ids"] = [sg.id]
    if desired_pairs:
        kwargs["allowed_address_pairs"] = desired_pairs
    port = conn.network.create_port(**kwargs)
    conn.network.set_tags(port, tags)
    return inv.put("ports", port)


def _reconcile_allowed_pairs(conn, port, desired_pairs):
    current = [
        {"ip_address": p["ip_address"]}
        for p in (getattr(port, "allowed_address_pairs", None) or [])
    ]
    if current == desired_pairs:
        return
    action(f"update allowed_address_pairs on port {port.name}")
    if dry_run():
        return
    conn.network.update_port(port, allowed_address_pairs=desired_pairs)


def _ensure_fip(conn, name, ext, port, inv, tags) -> str:
    """Ensure a floating ip described `name` is associated with `port`; return
    its address. Floating ips have no name field, so we key them by description
    (matching network.tf) in the inventory."""
    fip = inv.get("ips", name)
    if fip:
        info(f"floating ip {name} exists ({fip.floating_ip_address})")
        if port is not None and fip.port_id != port.id:
            action(f"associate floating ip {name} -> port {port.name}")
            if not dry_run():
                conn.network.update_ip(fip, port_id=port.id)
        return fip.floating_ip_address
    action(f"create floating ip {name}")
    if dry_run() or port is None:
        return ""
    fip = conn.network.create_ip(
        floating_network_id=ext.id,
        port_id=port.id,
        description=name,
    )
    try:
        conn.network.set_tags(fip, tags)
    except exceptions.SDKException:
        pass
    inv.put_keyed("ips", name, fip)
    return fip.floating_ip_address
