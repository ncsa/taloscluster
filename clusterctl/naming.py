"""Deterministic resource names + the tag convention.

Names are derived purely from cluster.yaml so every run computes the same name
for the same resource -- that determinism is what makes reconcile idempotent
without a state file. Tags let us enumerate exactly the resources this tool owns
(and only those), replacing terraform's state-held resource inventory.

Neutron/Nova/Cinder tags are plain strings, so we use a `key=value` convention.
"""

from __future__ import annotations

MANAGED_BY = "clusterctl"

# Extensions baked into every node's base image. tailscale gives reachability
# without public IPs (idles if no auth key); qemu-guest-agent lets OpenStack do
# graceful shutdown and report guest info to Nova. Neither is required to boot.
BASE_EXTENSIONS = ("siderolabs/tailscale", "siderolabs/qemu-guest-agent")


# ---- tags -----------------------------------------------------------------

def tag_managed() -> str:
    return f"managed-by={MANAGED_BY}"


def tag_cluster(cluster: str) -> str:
    return f"cluster={cluster}"


def tag_role(role: str) -> str:
    return f"role={role}"


def tag_pool(pool: str) -> str:
    return f"pool={pool}"


def base_tags(cluster: str) -> list[str]:
    """Tags applied to every resource; also the discovery filter for this cluster."""
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


# ---- boot image -----------------------------------------------------------
# One boot image per talos version, baked with the BASE_EXTENSIONS (tailscale +
# qemu-guest-agent). Fixed name for simplicity. Anything beyond the base set
# (e.g. a GPU pool's nvidia extensions) is carried in the node's install.image
# and applied on upgrade, not baked into the boot image.

def image_name(talos_version: str) -> str:
    return f"talos-{talos_version}-tailscale"
