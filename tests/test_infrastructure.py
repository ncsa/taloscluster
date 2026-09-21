"""Provider-neutral infrastructure types, selection, and address resolution."""

from __future__ import annotations

import pytest

from taloscluster.config import ConfigError
from taloscluster.infrastructure import (
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
    backend_for,
    dhcp_link_documents,
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


def test_node_address_ignores_an_unknown_discovered_address():
    """A discovered member whose only address was an excluded VIP reports ""
    (unknown); resolution must fall through to the real network/inventory
    address instead of addressing whichever node owns the VIP."""
    inventory = InfrastructureInventory(
        machines={
            "node-1": InfrastructureMachine(
                "node-1", attachments=(NetworkAttachment("private", "192.0.2.10"),)
            )
        }
    )

    assert resolve_node_address("node-1", {"node-1": ""}, inventory) == "192.0.2.10"


def test_dhcp_route_restatement_collides_with_the_lease_route():
    """The restated default route must replace, never duplicate, the route the
    DHCP lease provides. Talos identifies a route by table, destination,
    gateway, metric and link, and keeps the higher configuration layer when two
    routes collide, so the restated route carries only the gateway and the
    1500 MTU: any table, destination or metric of its own would give the lease
    route a different identity and leave two default routes in the kernel.
    Verified against Talos v1.13 on a DHCP node; see docs/concepts/machines.md.
    At the default MTU there is no clamp, so no route is restated at all even
    when a gateway is known.
    """
    link = dhcp_link_documents("eth0", mtu=9000, gateway="192.0.2.1")[0]
    assert link["mtu"] == 9000
    assert link["routes"] == [{"gateway": "192.0.2.1", "mtu": 1500}]

    assert dhcp_link_documents("eth0", mtu=1500, gateway="192.0.2.1")[0] == {
        "apiVersion": "v1alpha1",
        "kind": "LinkConfig",
        "name": "eth0",
    }


def test_proxmox_backend_is_selected(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "token_id": "user@pve!provider",
                "token_secret": "secret",
                "network": {
                    "cluster": {"bridge": "vmbr0"},
                },
            },
        },
        remove=("openstack",),
    )
    assert backend_for(cfg).name == "proxmox"


def test_backends_declare_a_talos_contribution_and_installer_platform():
    """Every backend supplies the Stage 4 hooks the shared generator calls."""
    from taloscluster.openstack.backend import OpenStackBackend
    from taloscluster.proxmox.backend import ProxmoxBackend

    assert OpenStackBackend.installer_platform == "openstack"
    assert ProxmoxBackend.installer_platform == "nocloud"
    for backend in (OpenStackBackend, ProxmoxBackend):
        assert callable(backend.talos_contribution)


def test_openstack_status_and_env_read_the_public_config(make_config, capsys, monkeypatch):
    """`status` and `env` take the provider url, region and credentials from
    the public Config accessors."""
    cfg = make_config({"openstack": {"credential_id": "app-cred", "credential_secret": "sekrit"}})
    from taloscluster.openstack.backend import OpenStackBackend

    monkeypatch.setattr("taloscluster.openstack.backend.project_name", lambda _conn: "proj")
    backend = object.__new__(OpenStackBackend)
    backend.cfg = cfg
    backend.conn = None
    assert backend.provider_status() == {
        "url": "https://example.com:5000/v3/",
        "region": "RegionOne",
        "project": "proj",
    }
    backend.print_environment()
    out = capsys.readouterr().out
    assert "export OS_AUTH_URL=https://example.com:5000/v3/" in out
    assert "export OS_APPLICATION_CREDENTIAL_ID=app-cred" in out


def test_proxmox_backend_contribution_rejects_anchor_collisions(make_config):
    from taloscluster.infrastructure import Endpoint
    from taloscluster.proxmox.backend import ProxmoxBackend

    cfg = make_config(
        {
            "controlplane": {"count": 2, "cores": 4, "memory": 8, "disk": 40},
            "network": {
                "external": {
                    "cidr": "203.0.113.0/24",
                    "gateway": "203.0.113.1",
                    # a /32 forces every machine onto the same anchor
                    "anchor_cidr": "169.254.40.1/32",
                    "kubeapi_vip": "203.0.113.10",
                },
            },
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0"},
                    "external": {"bridge": "vmbr1"},
                },
            },
        },
        remove=("openstack",),
    )
    backend = ProxmoxBackend(cfg, client=object())
    machine = cfg.machines["testcluster-controlplane-01"]

    with pytest.raises(ConfigError, match="anchor address collision"):
        backend.talos_contribution(machine, Endpoint(vip="203.0.113.10"))
