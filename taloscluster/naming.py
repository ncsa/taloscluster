"""Deterministic resource names + the tag convention.

Names are derived purely from cluster.yaml so every run computes the same name
for the same resource -- that determinism is what makes reconcile idempotent
without a state file. Tags let us enumerate exactly the resources this tool owns
(and only those), replacing terraform's state-held resource inventory.

Neutron/Nova/Cinder tags are plain strings, so we use a `key=value` convention.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re

from .errors import ConfigError

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


# ---- Proxmox SDN ------------------------------------------------------------
# The SDN zone and VNet share one id: `sdn.name`, defaulting to the cluster
# name. Proxmox limits these ids to 8 characters ([a-zA-Z][a-zA-Z0-9]*, no
# hyphens); config validation rejects an id that does not fit. Zones have no
# comment or alias field; the VNet alias is the only ownership carrier, and its
# character set forbids '=', so the marker differs from the pool-comment
# convention.

SDN_VNI_MIN = 1
SDN_VNI_MAX = 16777215
# Proxmox zone/vnet id format: 2-8 chars, letter first, no hyphens.
SDN_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]{1,7}$")


def sdn_alias(cluster: str) -> str:
    return f"managed-by taloscluster cluster {cluster}"


def sdn_vni(cluster: str) -> int:
    """Default VRF VXLAN id: even, so the default VNet tag (+1) stays in range."""
    return 10000 + 2 * (int(hashlib.sha256(cluster.encode()).hexdigest(), 16) % 8_000_000)


# ---- Proxmox SDN static addresses ------------------------------------------
# A managed EVPN network has no DHCP, so every node gets a deterministic
# address from network.cidr: the anycast gateway is the first host, control
# planes sit at 10+ordinal, and each worker pool gets a 50-address block in
# cluster.yaml order. Reordering or removing a worker pool renumbers the pools
# after it.

SDN_GATEWAY_HOST = 1
_SDN_CONTROLPLANE_BASE = 10
_SDN_WORKER_BASE = 60
_SDN_POOL_SIZE = 50


def sdn_gateway(cidr: str) -> ipaddress.IPv4Address:
    net = ipaddress.IPv4Network(cidr)
    return net.network_address + SDN_GATEWAY_HOST


def node_address(
    cidr: str, name: str, role: str, pool: str, worker_pools: tuple[str, ...]
) -> ipaddress.IPv4Interface:
    """Deterministic static node address (with prefix) on the cluster network."""
    net = ipaddress.IPv4Network(cidr)
    ordinal = int(name.rsplit("-", 1)[1])
    if role == "controlplane":
        if ordinal > _SDN_WORKER_BASE - _SDN_CONTROLPLANE_BASE - 1:
            raise ConfigError(
                f"machine {name}: too many control planes for the static SDN address layout"
            )
        host = _SDN_CONTROLPLANE_BASE + ordinal
    else:
        if pool not in worker_pools:
            raise ConfigError(f"machine {name}: unknown worker pool {pool!r}")
        if ordinal >= _SDN_POOL_SIZE:
            raise ConfigError(
                f"machine {name}: pool {pool!r} exceeds the static SDN address block "
                f"of {_SDN_POOL_SIZE} addresses"
            )
        host = _SDN_WORKER_BASE + _SDN_POOL_SIZE * worker_pools.index(pool) + ordinal
    address = net.network_address + host
    if address >= net.broadcast_address:
        raise ConfigError(
            f"machine {name}: static SDN address {address} does not fit network.cidr {cidr}"
        )
    return ipaddress.IPv4Interface(f"{address}/{net.prefixlen}")


def sdn_reserved(cidr: str, worker_pools: tuple[str, ...]) -> set:
    """Every address the static layout could assign, so a VIP can avoid it.

    Pools scale in place without renumbering, so the whole controlplane range
    and each worker pool's full block are reserved -- not just the slots nodes
    currently occupy.
    """
    net = ipaddress.IPv4Network(cidr)
    base = net.network_address
    reserved = {base + SDN_GATEWAY_HOST}
    reserved.update(
        base + host for host in range(_SDN_CONTROLPLANE_BASE, _SDN_WORKER_BASE)
    )
    for index in range(len(worker_pools)):
        start = _SDN_WORKER_BASE + index * _SDN_POOL_SIZE
        reserved.update(base + host for host in range(start, start + _SDN_POOL_SIZE))
    return reserved
