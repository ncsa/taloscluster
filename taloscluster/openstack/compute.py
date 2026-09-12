"""Reconcile compute instances, the port of nodes.tf.

Each server is volume-backed (a boot volume cloned from the shared Talos image,
delete_on_termination) with config_drive + the node's machine config as
user_data, attached to its pre-created port (which carries the security group
and the VIP allowed_address_pairs). No floating ip.

Create-only: like terraform's ignore_changes=[user_data, flavor_name,
block_device, availability_zone], an existing server is left untouched -- talos
and kubernetes upgrades happen via talosctl, never by replacing instances. A
change to a server's flavor, disk or availability zone cannot be reconciled in
place, so ``validate`` refuses it before any converge phase mutates; ``plan``
reports it the same way.

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


def validate(
    conn: Connection,
    cfg: Config,
    machines: dict[str, Machine],
    inv: Inventory,
) -> None:
    """Refuse a flavor, disk or availability-zone change on an existing server.

    Servers are create-only (see the module docstring), so any of these edits
    in cluster.yaml would otherwise be silently ignored while the plan claimed
    the cluster matches. Refuse before the image/network phases mutate.
    """
    for host, m in machines.items():
        server = inv.get("servers", host)
        if server is None:
            continue
        problems: list[str] = []
        if not _server_flavor_matches(conn, server, m.flavor):
            problems.append(f"flavor != configured {m.flavor}")
        current_disk = _server_boot_disk(conn, server)
        if current_disk is not None and current_disk != m.disk:
            problems.append(f"boot volume {current_disk}GB != configured {m.disk}GB")
        current_az = str(getattr(server, "availability_zone", "") or "")
        if current_az and current_az != cfg.availability_zone:
            problems.append(
                f"availability zone {current_az or '?'} != configured "
                f"{cfg.availability_zone}"
            )
        if not problems:
            continue
        raise ReconcileError(
            f"refusing unsupported change to existing server {host}: "
            + "; ".join(problems)
            + ". Servers are create-only on OpenStack; resize the node (or "
            "recreate it by scaling its pool down past it and back up) instead "
            "of editing cluster.yaml"
        )


def _server_flavor_matches(conn, server, configured: str) -> bool:
    """Whether the existing server's flavor matches the configured reference.

    The configured value may be a flavor name or id (``find_flavor`` accepts
    both at create time). Nova may report the flavor by name, by id, or both;
    each side is cross-resolved through the flavor catalog when only one is
    known. When the flavor cannot be read at all, the comparison is skipped
    (reported as matching) rather than refusing a healthy cluster.
    """
    flavor = getattr(server, "flavor", None)
    if flavor is None:
        return True
    configured = str(configured)
    name = str(getattr(flavor, "original_name", "") or getattr(flavor, "name", "") or "")
    flavor_id = str(getattr(flavor, "id", "") or "")
    if not name and not flavor_id:
        # nothing known about the running flavor -- skip the comparison
        return True
    if name and name == configured:
        return True
    if flavor_id and flavor_id == configured:
        return True
    if flavor_id and not name:
        # id-only reference: resolve the name to compare against a configured
        # name. If the catalog is unreadable we cannot tell whether they match,
        # so skip rather than refuse a healthy cluster.
        try:
            name = str(getattr(conn.compute.get_flavor(flavor_id), "name", "") or "")
        except Exception:
            return True
        return bool(name and name == configured)
    if name and not flavor_id:
        # name-only reference: resolve the id to compare against a configured id.
        try:
            flavor_id = str(getattr(conn.compute.find_flavor(name), "id", "") or "")
        except Exception:
            return True
        return bool(flavor_id and flavor_id == configured)
    return False


def _server_boot_disk(conn, server) -> int | None:
    """Size of the server's boot volume in GB, or None when it cannot be read."""
    attachments = getattr(server, "attached_volumes", None)
    if not attachments:
        attachments = getattr(server, "volumes", None)
    if not attachments:
        return None
    volume_id = _boot_volume_id(attachments)
    if not volume_id:
        return None
    try:
        volume = conn.volume.get_volume(volume_id)
    except Exception:
        return None
    size = getattr(volume, "size", None)
    return int(size) if size is not None else None


def _boot_volume_id(attachments) -> str | None:
    """The Cinder volume UUID backing the boot attachment.

    Nova's ``volumes_attached`` extension reports each mounted volume; newer
    replies carry the volume under ``volume_id`` (the attachment itself is
    ``id``), older ones only an ``id``. Prefer the attachment that looks like
    the boot disk -- an explicit ``boot_index`` of 0, or the volume marked to
    be deleted with the server -- before falling back to the first attachment
    that names a volume. This avoids mistaking a data volume listed first for
    the boot disk.
    """
    boot_signal: str | None = None
    first_named: str | None = None
    for attach in attachments:
        if isinstance(attach, dict):
            volume_id = attach.get("volume_id") or attach.get("volumeId")
            boot_index = attach.get("boot_index")
            delete_on_termination = attach.get("delete_on_termination")
        else:
            volume_id = getattr(attach, "volume_id", None) or getattr(
                attach, "volumeId", None
            )
            boot_index = getattr(attach, "boot_index", None)
            delete_on_termination = getattr(attach, "delete_on_termination", None)
        if not volume_id:
            continue
        if boot_index == 0:
            return volume_id
        if delete_on_termination and boot_signal is None:
            boot_signal = volume_id
        if first_named is None:
            first_named = volume_id
    if boot_signal is not None:
        return boot_signal
    if first_named is not None:
        return first_named
    for attach in attachments:
        attach_id = attach.get("id") if isinstance(attach, dict) else getattr(
            attach, "id", None
        )
        if attach_id:
            return attach_id
    return None


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
