"""Tests for the OpenStack Talos contribution.

OpenStack contributes only the virtio install disk and the legacy
``machine.network.interfaces`` block carrying the Layer 2 API VIP on eth0.
"""

from __future__ import annotations

import pytest

from taloscluster.infrastructure import Endpoint
from taloscluster.openstack import talos

VIP = "192.168.0.10"
FIP = "203.0.113.10"


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
    interfaces = contribution.patches[0].document["machine"]["network"]["interfaces"]
    assert interfaces == [{"interface": "eth0", "dhcp": True, "vip": {"ip": VIP}}]


def test_worker_gets_empty_interfaces(cfg, ep):
    m = cfg.machines["testcluster-worker-01"]
    contribution = talos.contribution(m, cfg, ep)
    assert contribution.patches[0].document["machine"]["network"]["interfaces"] == []


def test_installer_platform_is_openstack():
    assert talos.INSTALLER_PLATFORM == "openstack"
