"""Talos Image Factory client (https://factory.talos.dev).

A schematic pins the set of system extensions baked into an image; its id feeds
both the downloadable boot image and the `install.image` installer reference so
`talosctl upgrade` keeps (or drops) extensions to match.
"""

from __future__ import annotations

import requests
import yaml

FACTORY = "https://factory.talos.dev"
# openstack disk image is the installed system; this is the raw disk asset
IMAGE_ASSET = "openstack-amd64.raw.xz"
# the ISO boot media of every provider that boots one: the ISO only carries
# the machine to its configuration -- delivered by Proxmox's cidata seed,
# applied in maintenance mode on bare metal -- and the platform the machine
# installs and runs comes from the installer reference in that config, so
# metal machines boot this asset yet install the metal installer
NOCLOUD_ISO_ASSET = "nocloud-amd64.iso"
# the SecureBoot UKI ISO: its systemd-boot enrolls the factory's own keys into
# an efidisk whose varstore starts empty (Proxmox's pre-enrolled-keys=0 leaves
# the firmware in setup mode) and then boots Talos with Secure Boot enforced,
# all inside a virtual machine. On bare metal the enrollment is not automatic
# (systemd-boot only auto-enrolls under virtualization), so metal keeps the
# plain ISO.
NOCLOUD_SECUREBOOT_ISO_ASSET = "nocloud-amd64-secureboot.iso"


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
    secureboot: bool = False,
) -> str:
    """The installer image ref for `machine.install.image` (keeps extensions on
    upgrade). `secureboot` installs the SecureBoot (UKI) installer variant, for
    machines booted from a SecureBoot ISO."""
    if platform not in ("openstack", "nocloud", "metal"):
        raise ValueError(f"unsupported Talos installer platform: {platform}")
    suffix = "-secureboot" if secureboot else ""
    return f"factory.talos.dev/{platform}-installer{suffix}/{schematic}:{talos_version}"


def image_url(schematic: str, talos_version: str) -> str:
    """The downloadable openstack raw disk image (xz-compressed)."""
    return f"{FACTORY}/image/{schematic}/{talos_version}/{IMAGE_ASSET}"


def nocloud_iso_url(schematic: str, talos_version: str) -> str:
    return f"{FACTORY}/image/{schematic}/{talos_version}/{NOCLOUD_ISO_ASSET}"


def nocloud_secureboot_iso_url(schematic: str, talos_version: str) -> str:
    return f"{FACTORY}/image/{schematic}/{talos_version}/{NOCLOUD_SECUREBOOT_ISO_ASSET}"
