"""Talos Image Factory client (https://factory.talos.dev).

Replaces the `curl | jq` schematic POST in bin/cluster.sh. A schematic pins the
set of system extensions baked into an image; its id feeds both the downloadable
boot image and the `install.image` installer reference so `talosctl upgrade`
keeps (or drops) extensions to match.
"""

from __future__ import annotations

import requests
import yaml

FACTORY = "https://factory.talos.dev"
# openstack disk image is the installed system; this is the raw disk asset
IMAGE_ASSET = "openstack-amd64.raw.xz"
NOCLOUD_ISO_ASSET = "nocloud-amd64.iso"


def schematic_id(extensions) -> str:
    """POST the schematic for `extensions` and return its id (idempotent: the
    factory returns the same id for the same schematic)."""
    body = yaml.safe_dump(
        {
            "customization": {
                "systemExtensions": {
                    "officialExtensions": sorted(set(extensions)),
                }
            }
        },
        sort_keys=False,
    )
    resp = requests.post(
        f"{FACTORY}/schematics",
        data=body.encode(),
        headers={"Content-Type": "application/x-yaml"},
        timeout=30,
    )
    resp.raise_for_status()
    sid = resp.json()["id"]
    return sid


def installer_image(
    schematic: str,
    talos_version: str,
    platform: str = "openstack",
) -> str:
    """The installer image ref for `machine.install.image` (keeps extensions on
    upgrade)."""
    if platform not in ("openstack", "nocloud"):
        raise ValueError(f"unsupported Talos installer platform: {platform}")
    return f"factory.talos.dev/{platform}-installer/{schematic}:{talos_version}"


def image_url(schematic: str, talos_version: str) -> str:
    """The downloadable openstack raw disk image (xz-compressed)."""
    return f"{FACTORY}/image/{schematic}/{talos_version}/{IMAGE_ASSET}"


def nocloud_iso_url(schematic: str, talos_version: str) -> str:
    return f"{FACTORY}/image/{schematic}/{talos_version}/{NOCLOUD_ISO_ASSET}"
