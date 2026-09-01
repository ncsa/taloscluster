"""Proxmox spread and explicit placement."""

from __future__ import annotations

from dataclasses import replace

import pytest

from taloscluster.errors import ReconcileError
from taloscluster.proxmox.inventory import ProxmoxNode
from taloscluster.proxmox.placement import place


def test_spread_accounts_for_in_flight_memory(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 2, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 0, 16 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 16 * 1024**3),
    }

    placements = place(cfg.machines.values(), nodes)

    assert placements["testcluster-controlplane-01"] == "pve001"
    assert placements["testcluster-controlplane-02"] == "pve002"
    assert placements["testcluster-controlplane-03"] == "pve001"


def test_controlplanes_land_on_distinct_nodes(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 2, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 0, 16 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 16 * 1024**3),
        "pve003": ProxmoxNode("pve003", True, 0, 16 * 1024**3),
        "pve004": ProxmoxNode("pve004", True, 0, 16 * 1024**3),
    }

    placements = place(cfg.machines.values(), nodes)

    cp_nodes = [
        placements[name] for name in placements if "controlplane" in name
    ]
    assert len(cp_nodes) == len(set(cp_nodes))


def test_controlplanes_respect_existing_controlplane_nodes(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 2, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 0, 16 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 16 * 1024**3),
        "pve003": ProxmoxNode("pve003", True, 0, 16 * 1024**3),
    }
    remaining = [m for m in cfg.machines.values() if not m.name.endswith("-01")]

    placements = place(remaining, nodes, controlplane_nodes=frozenset({"pve001"}))

    assert placements["testcluster-controlplane-02"] != "pve001"
    assert placements["testcluster-controlplane-03"] != "pve001"
    assert placements["testcluster-controlplane-02"] != placements["testcluster-controlplane-03"]


def test_controlplanes_colocate_when_fewer_nodes_than_controlplanes(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 2, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 0, 16 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 16 * 1024**3),
    }

    placements = place(cfg.machines.values(), nodes)

    used = {placements[m.name] for m in cfg.machines.values()}
    assert used <= {"pve001", "pve002"}


def test_controlplanes_prefer_first_node_even_with_less_memory(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 2, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 20 * 1024**3, 32 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 32 * 1024**3),
        "pve003": ProxmoxNode("pve003", True, 0, 32 * 1024**3),
        "pve004": ProxmoxNode("pve004", True, 0, 32 * 1024**3),
    }

    placements = place(cfg.machines.values(), nodes)

    assert placements["testcluster-controlplane-01"] == "pve001"
    assert placements["testcluster-controlplane-02"] == "pve002"
    assert placements["testcluster-controlplane-03"] == "pve003"


def test_workers_still_spread_by_memory(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 2, "memory": 8, "disk": 40},
            "workers": {
                "worker": {"count": 2, "cores": 2, "memory": 8, "disk": 40},
            },
            "proxmox": {
                "url": "https://pve",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}
                },
            },
        },
        remove=("openstack",),
    )
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 20 * 1024**3, 32 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 32 * 1024**3),
        "pve003": ProxmoxNode("pve003", True, 0, 32 * 1024**3),
    }
    workers = [m for m in cfg.machines.values() if m.role == "worker"]

    placements = place(workers, nodes)

    assert placements["testcluster-worker-01"] == "pve002"
    assert placements["testcluster-worker-02"] == "pve003"


def test_explicit_node_is_honored(make_config):
    machine = replace(next(iter(make_config().machines.values())), memory=1, node="pve002")
    nodes = {
        "pve001": ProxmoxNode("pve001", True, 0, 16 * 1024**3),
        "pve002": ProxmoxNode("pve002", True, 0, 16 * 1024**3),
    }

    assert place([machine], nodes)[machine.name] == "pve002"


def test_offline_explicit_node_fails(make_config):
    machine = replace(next(iter(make_config().machines.values())), memory=1, node="pve002")

    with pytest.raises(ReconcileError, match="not online"):
        place([machine], {"pve002": ProxmoxNode("pve002", False)})
