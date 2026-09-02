"""Deterministic resource names + the tag convention.

Names are derived purely from cluster.yaml so every run computes the same name
for the same resource -- that determinism is what makes reconcile idempotent
without a state file. Tags let us enumerate exactly the resources this tool owns
(and only those), replacing terraform's state-held resource inventory.

Neutron/Nova/Cinder tags are plain strings, so we use a `key=value` convention.
"""

from __future__ import annotations

import hashlib

MANAGED_BY = "taloscluster"
# the tool was called clusterctl before; resources it created are still tagged
# managed-by=clusterctl. Discovery accepts either, new resources get MANAGED_BY.
LEGACY_MANAGED_BY = "clusterctl"

# Extensions baked into every node's base image. tailscale gives reachability
# without public IPs (idles if no auth key); qemu-guest-agent lets OpenStack do
# graceful shutdown and report guest info to Nova. Neither is required to boot.
BASE_EXTENSIONS = ("siderolabs/tailscale", "siderolabs/qemu-guest-agent")


# ---- tags -----------------------------------------------------------------

def tag_managed() -> str:
    return f"managed-by={MANAGED_BY}"


def managed_tags() -> list[str]:
    """Every managed-by value that marks a resource as ours."""
    return [tag_managed(), f"managed-by={LEGACY_MANAGED_BY}"]


def tag_cluster(cluster: str) -> str:
    return f"cluster={cluster}"


def tag_role(role: str) -> str:
    return f"role={role}"


def tag_pool(pool: str) -> str:
    return f"pool={pool}"


def base_tags(cluster: str) -> list[str]:
    """Tags applied to every resource this tool creates."""
    return [tag_managed(), tag_cluster(cluster)]


def node_tags(cluster: str, role: str, pool: str) -> list[str]:
    return base_tags(cluster) + [tag_role(role), tag_pool(pool)]


# ---- resource names -------------------------------------------------------

def network_name(cluster: str) -> str:
    return f"{cluster}-net"


def subnet_name(cluster: str) -> str:
    return f"{cluster}-subnet"


def router_name(cluster: str) -> str:
    return f"{cluster}-router"


def kubeapi_name(cluster: str) -> str:
    return f"{cluster}-kubeapi"


def ingress_name(cluster: str) -> str:
    return f"{cluster}-ingress"


def secgroup_name(cluster: str) -> str:
    return cluster


# server, boot volume and per-machine port all share the hostname as their name
def machine_name(hostname: str) -> str:
    return hostname


# ---- deterministic hardware addresses -------------------------------------
# Deterministic VirtIO MACs so Talos can select interfaces by permanent MAC
# instead of relying on kernel interface naming (eth0/eth1). Index 0 is the
# private cluster NIC, index 1 is the external NIC.

def mac_address(cluster: str, hostname: str, index: int) -> str:
    digest = hashlib.sha256(f"{cluster}/{hostname}/{index}".encode()).digest()
    octets = [0x02, digest[0], digest[1], digest[2], digest[3], digest[4]]
    return ":".join(f"{octet:02x}" for octet in octets)


# ---- boot image -----------------------------------------------------------
# One boot image per talos version, baked with the BASE_EXTENSIONS (tailscale +
# qemu-guest-agent). Fixed name for simplicity. Anything beyond the base set
# (e.g. a GPU pool's nvidia extensions) is carried in the node's install.image
# and applied on upgrade, not baked into the boot image.

def image_name(talos_version: str) -> str:
    return f"talos-{talos_version}-tailscale"
