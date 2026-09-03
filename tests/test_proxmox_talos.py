"""Tests for the Proxmox Talos contribution: anchor addressing, the native
network documents for the directly routed external NIC, and the conntrack
return-path static pod.

These moved out of ``test_machineconfig.py`` when the shared generator became
provider-neutral: everything here is Proxmox-owned.
"""

from __future__ import annotations

import pytest

from taloscluster.config import ConfigError
from taloscluster.infrastructure import Endpoint
from taloscluster.naming import mac_address
from taloscluster.proxmox import talos

VIP = "192.168.0.10"
FIP = "203.0.113.10"


@pytest.fixture
def ep() -> Endpoint:
    return Endpoint(vip=VIP, advertised_address=FIP)


def _proxmox_external_cfg(make_config):
    return make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "workers": {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 40}},
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
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                        "ingress_pool": "203.0.113.20-203.0.113.40",
                    },
                },
            },
        },
        remove=("openstack",),
    )


def test_anchor_address_is_deterministic():
    a1 = talos.anchor_address("169.254.40.0/24", "cluster", "node-01")
    a2 = talos.anchor_address("169.254.40.0/24", "cluster", "node-01")
    assert a1 == a2
    assert a1.startswith("169.254.40.")
    assert a1.endswith("/32")


def test_anchor_address_differs_per_host():
    a1 = talos.anchor_address("169.254.40.0/24", "cluster", "node-01")
    a2 = talos.anchor_address("169.254.40.0/24", "cluster", "node-02")
    assert a1 != a2


def test_anchor_addresses_rejects_collisions(make_config):
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
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    # a /32 anchor_cidr forces every machine onto the same address
    with pytest.raises(ConfigError, match="anchor address collision"):
        talos.anchor_addresses(
            "169.254.40.1/32", cfg.name, cfg.machines
        )


def test_contribution_uses_scsi_install_disk(make_config, ep):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    assert talos.contribution(m, cfg, ep).install_disk == "/dev/sda"


def test_contribution_without_external_uses_legacy_vip_interface(make_config, ep):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "workers": {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 40}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}},
            },
        },
        remove=("openstack",),
    )
    cp = talos.contribution(cfg.machines["testcluster-controlplane-01"], cfg, ep)
    worker = talos.contribution(cfg.machines["testcluster-worker-01"], cfg, ep)

    assert [p.name for p in cp.patches] == ["network"]
    interfaces = cp.patches[0].document["machine"]["network"]["interfaces"]
    assert interfaces == [{"interface": "eth0", "dhcp": True, "vip": {"ip": VIP}}]
    assert worker.patches[0].document["machine"]["network"]["interfaces"] == []


def test_contribution_with_external_has_network_and_return_path(make_config, ep):
    cfg = _proxmox_external_cfg(make_config)
    for host in cfg.machines:
        contribution = talos.contribution(cfg.machines[host], cfg, ep)
        assert [p.name for p in contribution.patches] == ["network", "return-path"]
        pod = contribution.patches[1].document["machine"]["pods"][0]
        assert pod["metadata"]["name"] == "taloscluster-proxmox-return-path"


def test_contribution_without_ingress_pool_has_no_return_path(make_config, ep):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
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
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    m = cfg.machines["testcluster-controlplane-01"]
    assert [p.name for p in talos.contribution(m, cfg, ep).patches] == ["network"]


def test_return_path_pod_selects_external_nic_by_mac(make_config):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-worker-01"]

    pod = talos.return_path_pod(m, cfg)

    container = pod["spec"]["containers"][0]
    script = container["command"][2]
    assert pod["metadata"]["name"] == "taloscluster-proxmox-return-path"
    assert pod["spec"]["hostNetwork"] is True
    assert container["image"] == f"registry.k8s.io/kube-proxy:{cfg.kubernetes_version}"
    assert container["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": ["NET_ADMIN"],
    }
    assert mac_address(cfg.name, m.name, 1).lower() in script
    assert "ip daddr 203.0.113.0/24" in script
    assert "ct direction original ct mark set" in script
    assert "ct direction reply" in script
    assert "meta mark set" in script
    assert 'iifname "eth1"' not in script
    assert "volumeMounts" not in container
    assert "volumes" not in pod["spec"]


def test_external_network_docs_controlplane_has_vip_and_routes(make_config):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    docs = talos.external_network_docs(m, cfg)
    kinds = [d["kind"] for d in docs]
    assert "LinkAliasConfig" in kinds  # private + external
    assert "DHCPv4Config" in kinds
    assert "LinkConfig" in kinds
    assert "RoutingRuleConfig" in kinds
    assert "Layer2VIPConfig" in kinds
    # routing rule sources the VIP and selects table 100
    rules = [d for d in docs if d["kind"] == "RoutingRuleConfig"]
    rule = next(d for d in rules if "src" in d)
    assert rule["src"] == "203.0.113.10/32"
    assert rule["table"] == "100"
    return_rule = next(d for d in rules if "fwMark" in d)
    assert return_rule["name"] == "1001"
    assert return_rule["fwMark"] == 0x2000
    assert return_rule["fwMask"] == 0x2000
    assert return_rule["table"] == "100"
    # VIP is on the external link
    vip = next(d for d in docs if d["kind"] == "Layer2VIPConfig")
    assert vip["name"] == "203.0.113.10"
    assert vip["link"] == "external"
    # external LinkConfig has routes in table 100
    ext_link = next(
        d for d in docs if d["kind"] == "LinkConfig" and d["name"] == "external"
    )
    assert ext_link["routes"][0]["table"] == "100"
    assert ext_link["routes"][1]["gateway"] == "203.0.113.1"


def test_external_network_docs_vip_on_private_link_when_in_cluster(make_config):
    """When kubeapi_vip is in network.cluster, the VIP and routes go on private, not external."""
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"},
                    "external": {
                        "bridge": "vmbr1",
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        "anchor_cidr": "169.254.40.0/24",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    m = cfg.machines["testcluster-controlplane-01"]
    docs = talos.external_network_docs(m, cfg)
    kinds = [d["kind"] for d in docs]
    # VIP on private link — no RoutingRuleConfig, no external routes
    assert "RoutingRuleConfig" not in kinds
    vip = next(d for d in docs if d["kind"] == "Layer2VIPConfig")
    assert vip["name"] == "192.168.0.10"
    assert vip["link"] == "private"
    ext_link = next(
        d for d in docs if d["kind"] == "LinkConfig" and d["name"] == "external"
    )
    assert "routes" not in ext_link


def test_external_network_docs_worker_has_no_vip_or_routes(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "workers": {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 40}},
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
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    m = cfg.machines["testcluster-worker-01"]
    docs = talos.external_network_docs(m, cfg)
    kinds = [d["kind"] for d in docs]
    assert "RoutingRuleConfig" not in kinds
    assert "Layer2VIPConfig" not in kinds
    # external LinkConfig has anchor address but no routes
    ext_link = next(
        d for d in docs if d["kind"] == "LinkConfig" and d["name"] == "external"
    )
    assert "routes" not in ext_link
    assert ext_link["addresses"][0]["address"].startswith("169.254.")


def test_external_network_docs_worker_has_return_path_routes_and_rule(make_config):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-worker-01"]
    docs = talos.external_network_docs(m, cfg)
    kinds = [d["kind"] for d in docs]
    assert "RoutingRuleConfig" in kinds
    assert "Layer2VIPConfig" not in kinds

    ext_link = next(
        d for d in docs if d["kind"] == "LinkConfig" and d["name"] == "external"
    )
    assert ext_link["routes"] == [
        {"destination": "203.0.113.0/24", "table": "100"},
        {"gateway": "203.0.113.1", "table": "100"},
    ]
    rule = next(d for d in docs if d["kind"] == "RoutingRuleConfig")
    assert rule == {
        "apiVersion": "v1alpha1",
        "kind": "RoutingRuleConfig",
        "name": "1001",
        "fwMark": 0x2000,
        "fwMask": 0x2000,
        "table": "100",
    }


def test_external_network_docs_select_nics_by_mac(make_config):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    docs = talos.external_network_docs(m, cfg)
    aliases = [d for d in docs if d["kind"] == "LinkAliasConfig"]
    private_alias = next(d for d in aliases if d["name"] == "private")
    external_alias = next(d for d in aliases if d["name"] == "external")
    assert mac_address(cfg.name, m.name, 0) in private_alias["selector"]["match"]
    assert mac_address(cfg.name, m.name, 1) in external_alias["selector"]["match"]


def _proxmox_sdn_cfg(make_config, extra_cluster: dict | None = None):
    cluster = {"sdn": {}, "kubeapi_vip": "192.168.0.9"}
    cluster.update(extra_cluster or {})
    return make_config(
        {
            "name": "testc",  # SDN ids are the cluster name (max 8 chars)
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "workers": {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 40}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "nodes": ["pve001", "pve002"],
                "network": {"cluster": cluster},
            },
        },
        remove=("openstack",),
    )


def test_contribution_sdn_without_external_uses_static_documents(make_config):
    cfg = _proxmox_sdn_cfg(make_config)
    endpoint = Endpoint(vip="192.168.0.9", advertised_address="192.168.0.9")
    cp = talos.contribution(cfg.machines["testc-controlplane-01"], cfg, endpoint)

    assert [p.name for p in cp.patches] == ["network", "nameservers"]
    docs = cp.patches[0].document
    kinds = [doc["kind"] for doc in docs]
    assert "DHCPv4Config" not in kinds
    link = next(doc for doc in docs if doc["kind"] == "LinkConfig")
    assert link["addresses"] == [{"address": "192.168.0.11/21"}]
    assert link["routes"] == [{"gateway": "192.168.0.1"}]
    vip_doc = next(doc for doc in docs if doc["kind"] == "Layer2VIPConfig")
    assert vip_doc == {
        "apiVersion": "v1alpha1",
        "kind": "Layer2VIPConfig",
        "name": "192.168.0.9",
        "link": "private",
    }
    assert cp.patches[1].document == {
        "machine": {"network": {"nameservers": ["1.1.1.1"]}}
    }


def test_contribution_sdn_worker_has_static_address_and_no_vip(make_config):
    cfg = _proxmox_sdn_cfg(make_config)
    endpoint = Endpoint(vip="192.168.0.9", advertised_address="192.168.0.9")
    wk = talos.contribution(cfg.machines["testc-worker-01"], cfg, endpoint)

    docs = wk.patches[0].document
    assert all(doc["kind"] != "Layer2VIPConfig" for doc in docs)
    link = next(doc for doc in docs if doc["kind"] == "LinkConfig")
    assert link["addresses"] == [{"address": "192.168.0.61/21"}]


def test_external_docs_with_sdn_replace_private_dhcp_with_static(make_config):
    cfg = make_config(
        {
            "name": "testc",
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {"sdn": {}},
                    "external": {
                        "bridge": "vmbr1",
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        "anchor_cidr": "169.254.40.0/24",
                        "kubeapi_vip": "203.0.113.10",
                    },
                },
            },
        },
        remove=("openstack",),
    )
    docs = talos.external_network_docs(cfg.machines["testc-controlplane-01"], cfg)

    kinds = [doc["kind"] for doc in docs]
    assert "DHCPv4Config" not in kinds
    private = next(
        doc for doc in docs if doc["kind"] == "LinkConfig" and doc["name"] == "private"
    )
    assert private["addresses"] == [{"address": "192.168.0.11/21"}]
    assert private["routes"] == [{"gateway": "192.168.0.1"}]
