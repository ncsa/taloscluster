"""Effective-permission preflight for every Proxmox mutating path."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ReconcileError

ISO_PRIVILEGES = frozenset(
    {"Datastore.Allocate", "Datastore.AllocateTemplate", "Datastore.Audit"}
)
VM_STORAGE_PRIVILEGES = frozenset({"Datastore.AllocateSpace", "Datastore.Audit"})
VM_PRIVILEGES = frozenset(
    {
        "VM.Allocate",
        "VM.Audit",
        "VM.PowerMgmt",
        "VM.GuestAgent.Audit",
        "VM.Config.CDROM",
        "VM.Config.CPU",
        "VM.Config.Disk",
        "VM.Config.HWType",
        "VM.Config.Memory",
        "VM.Config.Network",
        "VM.Config.Options",
    }
)


@dataclass(frozen=True)
class Requirement:
    path: str
    privileges: frozenset[str]


def requirements(
    *,
    iso_storage: str,
    cidata_storage: str,
    vm_storage: str,
    nodes: Iterable[str],
    network_path: str,
    vmids: Iterable[int] = (),
) -> tuple[Requirement, ...]:
    required = [
        Requirement("/", frozenset({"Pool.Allocate"})),
        Requirement(f"/storage/{iso_storage}", ISO_PRIVILEGES),
        Requirement(f"/storage/{cidata_storage}", ISO_PRIVILEGES),
        Requirement(f"/storage/{vm_storage}", VM_STORAGE_PRIVILEGES),
        Requirement("/vms", VM_PRIVILEGES),
        Requirement(network_path, frozenset({"SDN.Use"})),
    ]
    required.extend(
        Requirement(f"/nodes/{node}", frozenset({"Sys.Audit", "Sys.AccessNetwork"}))
        for node in sorted(set(nodes))
    )
    required.extend(Requirement(f"/vms/{vmid}", VM_PRIVILEGES) for vmid in sorted(set(vmids)))
    return tuple(required)


def validate_effective_permissions(
    effective: Mapping[str, Any],
    required: Iterable[Requirement],
) -> None:
    grants = {str(path): _privilege_names(value) for path, value in effective.items()}
    missing: list[tuple[str, list[str]]] = []
    for requirement in required:
        available: set[str] = set()
        for path, privileges in grants.items():
            if _contains(path, requirement.path):
                available.update(privileges)
        absent = sorted(requirement.privileges - available)
        if absent:
            missing.append((requirement.path, absent))
    if missing:
        detail = "; ".join(f"{path}: {', '.join(privileges)}" for path, privileges in missing)
        raise ReconcileError(f"Proxmox token is missing required effective permissions: {detail}")


def _privilege_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(name) for name, enabled in value.items() if enabled}
    if isinstance(value, list):
        return {str(name) for name in value}
    return set()


def _contains(grant_path: str, target_path: str) -> bool:
    if grant_path == "/":
        return target_path.startswith("/")
    return target_path == grant_path or target_path.startswith(grant_path.rstrip("/") + "/")
