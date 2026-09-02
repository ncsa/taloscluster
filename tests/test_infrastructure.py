"""Provider-neutral infrastructure types, selection, and address resolution."""

from __future__ import annotations

import pytest

from taloscluster.config import ConfigError, ProxmoxSecrets, Secrets
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


def test_proxmox_backend_is_selected(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {
                        "bridge": "vmbr0",
                        "kubeapi_vip": "192.168.0.10",
                    }
                },
            },
        },
        remove=("openstack",),
    )
    secrets = Secrets(
        provider=ProxmoxSecrets("user@pve!provider", "secret"),
        tailscale_auth_key=None,
    )

    assert backend_for(cfg, secrets).name == "proxmox"


def test_backends_declare_a_talos_contribution_and_installer_platform():
    """Every backend supplies the Stage 4 hooks the shared generator calls."""
    from taloscluster.openstack.backend import OpenStackBackend
    from taloscluster.proxmox.backend import ProxmoxBackend

    assert OpenStackBackend.installer_platform == "openstack"
    assert ProxmoxBackend.installer_platform == "nocloud"
    for backend in (OpenStackBackend, ProxmoxBackend):
        assert callable(backend.talos_contribution)


def test_proxmox_backend_contribution_rejects_anchor_collisions(make_config):
    from taloscluster.infrastructure import Endpoint
    from taloscluster.proxmox.backend import ProxmoxBackend

    cfg = make_config(
        {
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0"},
                    "external": {
                        "bridge": "vmbr1",
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        # a /32 forces every machine onto the same anchor
                        "anchor_cidr": "169.254.40.1/32",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    secrets = Secrets(provider=ProxmoxSecrets("user@pve!provider", "secret"))
    backend = ProxmoxBackend(cfg, secrets, client=object())
    machine = cfg.machines["testcluster-controlplane-01"]

    with pytest.raises(ConfigError, match="anchor address collision"):
        backend.talos_contribution(machine, Endpoint(vip="203.0.113.10"))
