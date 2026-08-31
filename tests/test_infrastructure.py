"""Provider-neutral infrastructure types, selection, and address resolution."""

from __future__ import annotations

import pytest

from taloscluster.config import ProxmoxSecrets, Secrets
from taloscluster.errors import ReconcileError
from taloscluster.infrastructure import (
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
    backend_for,
    resolve_node_address,
)


def test_node_address_prefers_talos_discovery():
    inventory = InfrastructureInventory(
        machines={
            "node-1": InfrastructureMachine(
                "node-1", attachments=(NetworkAttachment("private", "192.0.2.10"),)
            )
        }
    )
    network = NetworkResult(
        machine_attachments={
            "node-1": (NetworkAttachment("private", "192.0.2.11"),)
        }
    )

    assert resolve_node_address(
        "node-1", {"node-1": "100.64.0.10"}, inventory, network
    ) == "100.64.0.10"


def test_node_address_falls_back_to_network_then_inventory():
    inventory = InfrastructureInventory(
        machines={
            "node-1": InfrastructureMachine(
                "node-1", attachments=(NetworkAttachment("private", "192.0.2.10"),)
            )
        }
    )
    network = NetworkResult(
        machine_attachments={
            "node-1": (NetworkAttachment("private", "192.0.2.11"),)
        }
    )

    assert resolve_node_address("node-1", {}, inventory, network) == "192.0.2.11"
    assert resolve_node_address("node-1", {}, inventory) == "192.0.2.10"


def test_proxmox_backend_fails_with_stage_boundary(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8192, "disk": 40},
            "proxmox": {"url": "https://pve.example:8006/api2/json"},
        },
        remove=("openstack",),
    )
    secrets = Secrets(
        provider=ProxmoxSecrets("user@pve!provider", "secret"),
        tailscale_auth_key=None,
    )

    with pytest.raises(ReconcileError, match="Stage 2"):
        backend_for(cfg, secrets)
