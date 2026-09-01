"""Deterministic Proxmox node placement with in-flight memory reservations."""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Machine
from ..errors import ReconcileError
from .inventory import ProxmoxNode

_GIB = 1024**3


def place(
    machines: Iterable[Machine],
    nodes: dict[str, ProxmoxNode],
    *,
    allowed_nodes: tuple[str, ...] = (),
    controlplane_nodes: frozenset[str] = frozenset(),
) -> dict[str, str]:
    ordered = sorted(machines, key=lambda item: item.name)
    eligible = {
        name: node
        for name, node in nodes.items()
        if node.online and (not allowed_nodes or name in allowed_nodes)
    }
    if not eligible:
        explicit = next((machine for machine in ordered if machine.node), None)
        if explicit is not None:
            raise ReconcileError(
                f"Proxmox node {explicit.node!r} for {explicit.name} is not online or allowed"
            )
        raise ReconcileError("no online Proxmox nodes are eligible for placement")

    reserved = {name: 0 for name in eligible}
    used_controlplane = set(controlplane_nodes)
    placements: dict[str, str] = {}
    for machine in ordered:
        needed = machine.memory * _GIB
        if machine.node:
            if machine.node not in eligible:
                raise ReconcileError(
                    f"Proxmox node {machine.node!r} for {machine.name} is not online or allowed"
                )
            chosen = machine.node
        else:
            choices = [
                name
                for name, node in eligible.items()
                if node.memory_available - reserved[name] >= needed
            ]
            if not choices:
                raise ReconcileError(
                    f"no Proxmox node has {machine.memory} GB available for {machine.name}"
                )
            if machine.role == "controlplane":
                free = [name for name in choices if name not in used_controlplane]
                if free:
                    choices = free
                chosen = min(choices)
            else:
                chosen = min(
                    choices,
                    key=lambda name: (-(eligible[name].memory_available - reserved[name]), name),
                )
        if eligible[chosen].memory_available - reserved[chosen] < needed:
            raise ReconcileError(
                f"Proxmox node {chosen!r} lacks {machine.memory} GB for {machine.name}"
            )
        placements[machine.name] = chosen
        reserved[chosen] += needed
        if machine.role == "controlplane":
            used_controlplane.add(chosen)
    return placements
