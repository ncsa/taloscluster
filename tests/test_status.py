"""Tests for converge.status -- the report and its text output.

The provider backend and the cluster probes are stubbed at the converge
boundary, so the tests pin what the report carries (the metal machines
included) without any cloud, talosctl or kubectl access.
"""

from __future__ import annotations

import pytest

from taloscluster import converge
from taloscluster.infrastructure import (
    Endpoint,
    InfrastructureInventory,
    NetworkResult,
)

PROXMOX = {
    "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
    "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
    "proxmox": {
        "url": "https://pve.example:8006",
        "storage": "vms",
        "iso_storage": "isos",
        "network": {"cluster": {"bridge": "vmbr0"}},
    },
}


def _metal(servers: dict) -> dict:
    return {
        "site": {
            "role": "worker",
            "disk": "/dev/sda",
            "network": {"cidr": "192.168.0.0/21", "gateway": "192.168.0.1"},
            "interfaces": {"enp1s0f0": {"role": "cluster"}},
            "servers": servers,
        },
    }


class FakeBackend:
    """The read-only surface status_report touches, with no cloud behind it."""

    name = "proxmox"

    def load_inventory(self) -> InfrastructureInventory:
        return InfrastructureInventory(
            resources={"vms": ["testcluster-controlplane-01"]},
        )

    def current_network(self, _inv) -> NetworkResult:
        return NetworkResult(
            kubernetes=Endpoint(vip="192.168.0.10", advertised_address="192.168.0.10"),
            ingress=Endpoint(vip="192.168.0.11", advertised_address="192.168.0.11"),
        )

    def provider_status(self) -> dict:
        return {"url": "https://pve.example:8006", "online_nodes": ["pve-01"]}


@pytest.fixture
def report(make_config, tmp_path, monkeypatch):
    """Run status_report against a stubbed provider with the kube-api down."""
    def _build(overrides: dict) -> dict:
        make_config({**PROXMOX, **overrides}, remove=("openstack",))
        monkeypatch.setattr(converge, "backend_for", lambda _cfg: FakeBackend())
        monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
        return converge.status_report(tmp_path)
    return _build


def test_report_lists_the_metal_servers(report):
    """No provider manages the metal machines, so they show up in neither the
    inventory resources nor the kube node list: status reads them straight
    from cluster.yaml -- the machines check verifies the cluster against."""
    metal = report({
        "metal": _metal({
            "rp001": {"interfaces": {"enp1s0f0": {"ip": "192.168.0.5/21"}}},
            "rp002": {
                "role": "controlplane",
                "interfaces": {"enp1s0f0": {"ip": "192.168.0.6/21"}},
            },
        }),
    })
    assert metal["metal"] == {"rp001": "worker", "rp002": "controlplane"}
    # the provider-managed half is unchanged
    assert metal["resources"] == {"vms": ["testcluster-controlplane-01"]}
    assert metal["nodes"] == []


def test_report_has_an_empty_metal_section_without_a_metal_config(report):
    assert report({})["metal"] == {}


def test_text_output_names_the_metal_servers(
    make_config, tmp_path, monkeypatch, capsys
):
    make_config(
        {**PROXMOX, "metal": _metal(
            {"rp001": {"interfaces": {"enp1s0f0": {"ip": "192.168.0.5/21"}}}},
        )},
        remove=("openstack",),
    )
    monkeypatch.setattr(converge, "backend_for", lambda _cfg: FakeBackend())
    monkeypatch.setattr(converge.kubectl, "cluster_up", lambda _kc: False)
    monkeypatch.setattr(converge.plugins, "active", lambda _ctx: [])
    converge.status(tmp_path)
    out = capsys.readouterr().out
    assert "==> metal" in out
    assert "    rp001: worker" in out
