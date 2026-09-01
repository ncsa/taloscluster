"""Bulk Proxmox discovery and ownership classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import ProxmoxClient


@dataclass(frozen=True)
class ProxmoxNode:
    name: str
    online: bool
    memory_used: int = 0
    memory_total: int = 0

    @property
    def memory_available(self) -> int:
        return max(self.memory_total - self.memory_used, 0)


@dataclass(frozen=True)
class ProxmoxVM:
    vmid: int
    name: str
    node: str
    status: str = ""
    pool: str = ""
    tags: frozenset[str] = frozenset()
    memory: int = 0


@dataclass(frozen=True)
class ProxmoxPool:
    poolid: str
    comment: str = ""


@dataclass
class ProxmoxInventory:
    nodes: dict[str, ProxmoxNode] = field(default_factory=dict)
    storages: dict[str, dict[str, Any]] = field(default_factory=dict)
    pools: dict[str, ProxmoxPool] = field(default_factory=dict)
    vms: dict[str, ProxmoxVM] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)


def load(client: ProxmoxClient) -> ProxmoxInventory:
    nodes = {
        str(item["node"]): ProxmoxNode(
            name=str(item["node"]),
            online=item.get("status") == "online",
            memory_used=int(item.get("mem") or 0),
            memory_total=int(item.get("maxmem") or 0),
        )
        for item in _items(client.get("nodes"))
        if item.get("node")
    }
    storages = {
        str(item["storage"]): dict(item)
        for item in _items(client.get("storage"))
        if item.get("storage")
    }
    pools = {
        str(item["poolid"]): ProxmoxPool(
            poolid=str(item["poolid"]),
            comment=str(item.get("comment") or ""),
        )
        for item in _items(client.get("pools"))
        if item.get("poolid")
    }
    vms: dict[str, ProxmoxVM] = {}
    for item in _items(client.get("cluster/resources", params={"type": "vm"})):
        if item.get("type") not in (None, "qemu") or not item.get("name"):
            continue
        vm = ProxmoxVM(
            vmid=int(item["vmid"]),
            name=str(item["name"]),
            node=str(item.get("node") or ""),
            status=str(item.get("status") or ""),
            pool=str(item.get("pool") or ""),
            tags=_tags(item.get("tags")),
            memory=int(item.get("maxmem") or 0),
        )
        vms[vm.name] = vm
    permissions = client.get("access/permissions")
    return ProxmoxInventory(
        nodes=nodes,
        storages=storages,
        pools=pools,
        vms=vms,
        permissions=dict(permissions) if isinstance(permissions, dict) else {},
    )


def owned_tags(cluster: str, role: str, pool: str) -> frozenset[str]:
    return frozenset(
        {"taloscluster", f"cluster_{cluster}", f"role_{role}", f"pool_{pool}"}
    )


def is_owned(vm: ProxmoxVM, cluster: str) -> bool:
    return "taloscluster" in vm.tags and f"cluster_{cluster}" in vm.tags


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _tags(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(tag for tag in value.split(";") if tag)
    if isinstance(value, list):
        return frozenset(str(tag) for tag in value)
    return frozenset()
