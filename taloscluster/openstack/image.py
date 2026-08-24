"""Ensure the Talos boot image exists -- one per talos version.

On OpenStack the disk image IS the running system at first boot, so the boot
image is baked with the BASE_EXTENSIONS (tailscale + qemu-guest-agent). Anything
beyond that (e.g. a GPU pool's nvidia extensions) is NOT in the boot image -- it
lives in the node's install.image and loads on a later `talosctl upgrade`.

This replaces the curl + xz + `openstack image create` block of bin/cluster.sh
with requests + lzma + glance. Images are shared across clusters (same name =
same content) and NEVER deleted here.
"""

from __future__ import annotations

import lzma
import shutil
import tempfile
from pathlib import Path

import requests
from openstack.connection import Connection

from .. import naming
from ..config import Config
from ..output import action, dry_run, info
from ..talos import factory

MIN_DISK_GB = 20
MIN_RAM_MB = 2048
_CHUNK = 8 * 1024 * 1024


# image properties. hw_qemu_guest_agent=yes makes Nova/libvirt attach the
# guest-agent virtio-serial channel, without which the baked qemu-guest-agent
# extension crash-loops and the node never leaves the "booting" stage.
IMAGE_PROPERTIES = {"hw_qemu_guest_agent": "yes"}


def ensure_image(conn: Connection, cfg: Config) -> str:
    """Ensure the single boot image exists (with the right properties); return
    its name.

    Built from BASE_EXTENSIONS (tailscale + qemu-guest-agent). All nodes boot
    from this one image regardless of pool.
    """
    name = naming.image_name(cfg.talos_version)
    existing = conn.image.find_image(name)
    if existing:
        info(f"image {name} exists")
        _ensure_properties(conn, existing)
        return name
    _build_image(conn, cfg.talos_version, naming.BASE_EXTENSIONS, name)
    return name


def _ensure_properties(conn: Connection, img) -> None:
    """Make sure a pre-existing image carries the required properties (e.g. an
    image built before hw_qemu_guest_agent was added)."""
    props = getattr(img, "properties", None) or {}
    missing = {
        k: v for k, v in IMAGE_PROPERTIES.items()
        if str(props.get(k, getattr(img, k, None))) != v
    }
    if not missing:
        info("image properties ok")
        return
    action(f"set image properties on {img.name}: {missing}")
    if not dry_run():
        conn.image.update_image(img, **missing)


def _build_image(conn: Connection, talos_version: str, ext_set, name: str) -> None:
    sid = factory.schematic_id(ext_set)
    url = factory.image_url(sid, talos_version)
    action(f"build image {name} from schematic {sid}")
    if dry_run():
        return

    workdir = Path(tempfile.mkdtemp(prefix="taloscluster-image-"))
    try:
        raw = workdir / "talos.raw"
        _download_and_decompress(url, raw)
        info(f"uploading {name} to glance ({raw.stat().st_size // (1024*1024)} MB)")
        conn.image.create_image(
            name=name,
            filename=str(raw),
            disk_format="raw",
            container_format="bare",
            min_disk=MIN_DISK_GB,
            min_ram=MIN_RAM_MB,
            **IMAGE_PROPERTIES,  # type: ignore[arg-type]  # free-form glance properties
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _download_and_decompress(url: str, dest_raw: Path) -> None:
    """Stream the .raw.xz from the factory and lzma-decompress it to dest_raw,
    without holding the whole image in memory."""
    with requests.get(url, stream=True, timeout=(30, 600)) as resp:
        resp.raise_for_status()
        decomp = lzma.LZMADecompressor()
        n = 0
        with open(dest_raw, "wb") as out:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if chunk:
                    n += len(chunk)
                    out.write(decomp.decompress(chunk))
        expected = resp.headers.get("Content-Length")
        if expected is not None and n != int(expected):
            raise RuntimeError(f"truncated download from {url}: got {n} of {expected} bytes")
        if not decomp.eof:
            raise RuntimeError(
                f"truncated download from {url} -- refusing to upload a corrupt image"
            )
