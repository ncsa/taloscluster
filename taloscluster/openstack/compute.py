"""Reconcile compute instances, the port of nodes.tf.

Each server is volume-backed (a boot volume cloned from the shared Talos image,
delete_on_termination) with config_drive + the node's machine config as
user_data, attached to its pre-created port (which carries the security group
and the VIP allowed_address_pairs). No floating ip.

Create-only: like terraform's ignore_changes=[user_data, flavor_name,
block_device, availability_zone], an existing server is left untouched -- talos
and kubernetes upgrades happen via talosctl, never by replacing instances.

Scale-down deletion is driven by converge (after drain + talos reset); here we
just delete the server and its port. The boot volume goes with the server via
delete_on_termination.
"""

from __future__ import annotations

import base64

from openstack.connection import Connection

from .. import naming
from ..config import Config, Machine
from ..errors import ReconcileError
from ..output import action, dry_run, info
from .session import Inventory


def reconcile(
    conn: Connection,
    cfg: Config,
    machines: dict[str, Machine],
    inv: Inventory,
    boot_image: str,
    configs: dict[str, str],
) -> None:
    for host, m in machines.items():
        if inv.get("servers", host):
            info(f"server {host} exists")
            continue
        _create_server(conn, cfg, m, inv, boot_image, configs)


def _create_server(conn, cfg: Config, m: Machine, inv, boot_image: str, configs) -> None:
    action(f"create server {m.name} ({m.flavor}, {m.disk}GB)")
    if dry_run():
        return

    flavor = conn.compute.find_flavor(m.flavor)
    if flavor is None:
        raise ReconcileError(f"flavor '{m.flavor}' not found")
    img = conn.image.find_image(boot_image)
    if img is None:
        raise ReconcileError(f"image '{boot_image}' not found (build phase failed?)")
    port = inv.get("ports", naming.machine_name(m.name))
    if port is None:
        raise ReconcileError(f"port for {m.name} missing (network phase failed?)")

    user_data = base64.b64encode(configs[m.name].encode()).decode()

    server = conn.compute.create_server(
        name=m.name,
        flavor_id=flavor.id,
        availability_zone=cfg.availability_zone,
        config_drive=True,
        user_data=user_data,
        networks=[{"port": port.id}],
        block_device_mapping=[
            {
                "boot_index": 0,
                "uuid": img.id,
                "source_type": "image",
                "destination_type": "volume",
                "volume_size": m.disk,
                "delete_on_termination": True,
            }
        ],
        tags=naming.node_tags(cfg.name, m.role, m.pool),
    )
    inv.put("servers", server)


def delete_node(conn: Connection, host: str, inv: Inventory) -> None:
    """Delete a scaled-down node's server (boot volume follows) and its port."""
    server = inv.get("servers", host)
    if server is not None:
        action(f"delete server {host}")
        if not dry_run():
            conn.compute.delete_server(server.id)
            conn.compute.wait_for_delete(server)
        inv.drop("servers", host)

    port = inv.get("ports", host)
    if port is not None:
        action(f"delete port {host}")
        if not dry_run():
            conn.network.delete_port(port.id)
        inv.drop("ports", host)
