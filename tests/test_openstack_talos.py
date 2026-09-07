"""Tests for the OpenStack Talos contribution.

OpenStack contributes only the virtio install disk and the network documents
that keep DHCP on eth0 and carry the Layer 2 API VIP there.
"""

from __future__ import annotations

import pytest

from taloscluster.infrastructure import Endpoint
from taloscluster.openstack import talos

VIP = "192.168.0.10"
FIP = "203.0.113.10"
ETH0_DHCP = [
    {"apiVersion": "v1alpha1", "kind": "LinkConfig", "name": "eth0"},
    {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": "eth0"},
]


@pytest.fixture
def ep() -> Endpoint:
    return Endpoint(vip=VIP, advertised_address=FIP)


@pytest.fixture
def cfg(make_config):
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
    })


def test_contribution_uses_virtio_install_disk(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    assert talos.contribution(m, cfg, ep).install_disk == "/dev/vda"


def test_controlplane_gets_the_vip_on_eth0(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    contribution = talos.contribution(m, cfg, ep)

    assert [p.name for p in contribution.patches] == ["network"]
    assert contribution.patches[0].document == ETH0_DHCP + [
        {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": VIP, "link": "eth0"},
    ]


def test_worker_keeps_dhcp_without_a_vip(cfg, ep):
    m = cfg.machines["testcluster-worker-01"]
    contribution = talos.contribution(m, cfg, ep)
    assert contribution.patches[0].document == ETH0_DHCP


def test_installer_platform_is_openstack():
    assert talos.INSTALLER_PLATFORM == "openstack"
