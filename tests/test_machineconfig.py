"""Tests for taloscluster.talos.machineconfig: the per-node patch builders and
the ``build_configs`` orchestrator.

No ``talosctl`` binary is needed: ``build_configs`` shells out via
``talosctl.gen_config``, which we monkeypatch to capture its kwargs and return
a sentinel string. The patch builders are pure dict constructors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from taloscluster.config import ConfigError, ProxmoxSecrets, Secrets
from taloscluster.infrastructure import Endpoint
from taloscluster.naming import mac_address
from taloscluster.talos import machineconfig
from taloscluster.talos.machineconfig import INSTALL_DISKS

FIP = "203.0.113.10"
VIP = "192.168.0.10"
INSTALLER = "factory.talos.dev/openstack-installer/abc123:v1.8.3"


@pytest.fixture
def cfg(make_config):
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "tailscale": {"login_server": "https://headscale.example.com"},
    })


@pytest.fixture
def ep() -> Endpoint:
    return Endpoint(vip=VIP, advertised_address=FIP)


# ---------------------------------------------------------------------------
# _machine_patch
# ---------------------------------------------------------------------------

def test_machine_patch_controlplane_has_vip_interface(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    interfaces = patch["machine"]["network"]["interfaces"]
    assert len(interfaces) == 1
    assert interfaces[0] == {"interface": "eth0", "dhcp": True, "vip": {"ip": VIP}}


def test_machine_patch_worker_has_empty_interfaces(cfg, ep):
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert patch["machine"]["network"]["interfaces"] == []


def test_machine_patch_certsans_contains_fip(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert FIP in patch["machine"]["certSANs"]


def test_machine_patch_install_image_is_installer_ref(cfg, ep):
    for host in cfg.machines:
        m = cfg.machines[host]
        patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
        assert patch["machine"]["install"]["image"] == INSTALLER
        assert patch["machine"]["install"]["disk"] == INSTALL_DISKS["openstack"]
        assert patch["machine"]["install"]["wipe"] is True


def test_machine_patch_proxmox_installs_to_scsi_disk(make_config, ep):
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
    machine = cfg.machines["testcluster-controlplane-01"]

    patch = machineconfig._machine_patch(machine, cfg, ep, INSTALLER)

    assert patch["machine"]["install"]["disk"] == INSTALL_DISKS["proxmox"]


def test_machine_patch_nodelabels_carry_role_and_pool(cfg, ep):
    cp = cfg.machines["testcluster-controlplane-01"]
    wk = cfg.machines["testcluster-worker-01"]
    cp_patch = machineconfig._machine_patch(cp, cfg, ep, INSTALLER)
    wk_patch = machineconfig._machine_patch(wk, cfg, ep, INSTALLER)
    assert cp_patch["machine"]["nodeLabels"] == {
        "ncsa/role": "controlplane", "ncsa/pool": "controlplane"
    }
    assert wk_patch["machine"]["nodeLabels"] == {"ncsa/role": "worker", "ncsa/pool": "worker"}


def test_machine_patch_nodelabels_include_tags_and_defaults(make_config, ep):
    cfg = make_config({
        "tags": {"team": "platform"},
        "workers": {"worker": {
            "count": 1, "flavor": "gp.xlarge", "disk": 50,
            "tags": {"workload": "batch"},
        }},
    })
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER,
                                         default_tags={"ncsa/project": "my project"})
    assert patch["machine"]["nodeLabels"] == {
        "ncsa/role": "worker",
        "ncsa/pool": "worker",
        "ncsa/project": "my_project",  # spaces in the project name become _
        "team": "platform",
        "workload": "batch",
    }


def test_machine_patch_user_tag_overrides_default(make_config, ep):
    cfg = make_config({"tags": {"ncsa/project": "override"}})
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER,
                                         default_tags={"ncsa/project": "bbdb"})
    assert patch["machine"]["nodeLabels"]["ncsa/project"] == "override"


def test_machine_patch_kubelet_node_ip_pinned_to_cidr(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert patch["machine"]["kubelet"]["nodeIP"]["validSubnets"] == [cfg.cidr]
    assert patch["machine"]["kubelet"]["extraArgs"]["rotate-server-certificates"] is True


def test_machine_patch_time_servers_from_cfg(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert patch["machine"]["time"]["servers"] == cfg.ntp


# ---------------------------------------------------------------------------
# _hostname_patch
# ---------------------------------------------------------------------------

def test_hostname_patch_kind_and_hostname(cfg):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._hostname_patch(m)
    assert patch["kind"] == "HostnameConfig"
    assert patch["hostname"] == m.name


def test_hostname_patch_auto_is_patch_delete(cfg):
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._hostname_patch(m)
    assert patch["auto"] == {"$patch": "delete"}


# ---------------------------------------------------------------------------
# _tailscale_patch
# ---------------------------------------------------------------------------

def test_tailscale_patch_env_lines(cfg):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._tailscale_patch(m, cfg, "tskey-secret")
    env = patch["environment"]
    assert patch["kind"] == "ExtensionServiceConfig"
    assert patch["name"] == "tailscale"
    assert "TS_AUTHKEY=tskey-secret" in env
    assert f"TS_HOSTNAME={m.name}" in env
    extra = [line for line in env if line.startswith("TS_EXTRA_ARGS=")]
    assert len(extra) == 1
    # login server appears in TS_EXTRA_ARGS
    assert cfg.login_server in extra[0]


# ---------------------------------------------------------------------------
# build_configs
# ---------------------------------------------------------------------------

def _installer_images(cfg):
    return {
        ext_set: INSTALLER
        for ext_set in cfg.extension_sets()
    }


def test_build_configs_one_entry_per_machine(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets = Secrets(
        openstack_credential_id="id",
        openstack_credential_secret="secret",
        tailscale_auth_key="tskey-secret",
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    configs = machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    assert set(configs.keys()) == set(cfg.machines.keys())
    assert all(v == "CONFIG" for v in configs.values())
    # one gen_config call per machine
    assert len(calls) == len(cfg.machines)


def test_build_configs_output_type_matches_role(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets = Secrets(
        openstack_credential_id="id",
        openstack_credential_secret="secret",
        tailscale_auth_key="tskey-secret",
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    for call, (_host, m) in zip(calls, cfg.machines.items(), strict=True):
        expected = "controlplane" if m.role == "controlplane" else "worker"
        assert call["output_type"] == expected
        assert call["install_disk"] == INSTALL_DISKS["openstack"]


def test_build_configs_passes_proxmox_scsi_disk_to_talosctl(
    make_config, monkeypatch, tmp_path, ep,
):
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
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets = Secrets(
        provider=ProxmoxSecrets("user@pve!provider", "secret"),
        tailscale_auth_key=None,
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg,
        secrets,
        cfg.machines,
        endpoint=ep,
        secrets_path=secrets_path,
        installer_images=_installer_images(cfg),
    )

    assert calls
    assert all(call["install_disk"] == INSTALL_DISKS["proxmox"] for call in calls)


def test_build_configs_tailscale_patch_present_when_key_set(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets = Secrets(
        openstack_credential_id="id",
        openstack_credential_secret="secret",
        tailscale_auth_key="tskey-secret",
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    for call, host in zip(calls, cfg.machines.keys(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        assert f"{host}-tailscale.yaml" in patch_names


def test_build_configs_no_tailscale_patch_when_key_absent(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets = Secrets(
        openstack_credential_id="id",
        openstack_credential_secret="secret",
        tailscale_auth_key=None,
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    for call, host in zip(calls, cfg.machines.keys(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        assert f"{host}-tailscale.yaml" not in patch_names


def test_build_configs_cluster_patch_only_for_controlplane(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets = Secrets(
        openstack_credential_id="id",
        openstack_credential_secret="secret",
        tailscale_auth_key=None,
    )
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    for call, (host, m) in zip(calls, cfg.machines.items(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        if m.role == "controlplane":
            assert f"{host}-cluster.yaml" in patch_names
        else:
            assert f"{host}-cluster.yaml" not in patch_names


# ---------------------------------------------------------------------------
# Proxmox external network documents
# ---------------------------------------------------------------------------

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
    a1 = machineconfig._anchor_address("169.254.40.0/24", "cluster", "node-01")
    a2 = machineconfig._anchor_address("169.254.40.0/24", "cluster", "node-01")
    assert a1 == a2
    assert a1.startswith("169.254.40.")
    assert a1.endswith("/32")


def test_anchor_address_differs_per_host():
    a1 = machineconfig._anchor_address("169.254.40.0/24", "cluster", "node-01")
    a2 = machineconfig._anchor_address("169.254.40.0/24", "cluster", "node-02")
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
        machineconfig._anchor_addresses(
            "169.254.40.1/32", cfg.name, cfg.machines
        )


def test_machine_patch_omits_legacy_interfaces_when_external(make_config, ep):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert "network" not in patch["machine"]


def test_machine_patch_worker_adds_return_path_static_pod(make_config, ep):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)

    pod = patch["machine"]["pods"][0]
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


def test_machine_patch_controlplane_adds_return_path_static_pod(make_config, ep):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    pod = patch["machine"]["pods"][0]
    assert pod["metadata"]["name"] == "taloscluster-proxmox-return-path"


def test_machine_patch_keeps_legacy_interfaces_without_external(make_config, ep):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}},
            },
        },
        remove=("openstack",),
    )
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER)
    assert "network" in patch["machine"]
    assert patch["machine"]["network"]["interfaces"][0]["vip"]["ip"] == VIP


def test_external_network_docs_controlplane_has_vip_and_routes(make_config):
    cfg = _proxmox_external_cfg(make_config)
    m = cfg.machines["testcluster-controlplane-01"]
    docs = list(_yaml.safe_load_all(machineconfig._proxmox_external_network_docs(m, cfg)))
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
    docs = list(_yaml.safe_load_all(machineconfig._proxmox_external_network_docs(m, cfg)))
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
    docs = list(_yaml.safe_load_all(machineconfig._proxmox_external_network_docs(m, cfg)))
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
    docs = list(_yaml.safe_load_all(machineconfig._proxmox_external_network_docs(m, cfg)))
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
    docs = list(_yaml.safe_load_all(machineconfig._proxmox_external_network_docs(m, cfg)))
    aliases = [d for d in docs if d["kind"] == "LinkAliasConfig"]
    private_alias = next(d for d in aliases if d["name"] == "private")
    external_alias = next(d for d in aliases if d["name"] == "external")
    assert mac_address(cfg.name, m.name, 0) in private_alias["selector"]["match"]
    assert mac_address(cfg.name, m.name, 1) in external_alias["selector"]["match"]


def test_build_configs_includes_network_patch_when_external(make_config, monkeypatch, tmp_path):
    cfg = _proxmox_external_cfg(make_config)
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets = Secrets(provider=ProxmoxSecrets("user@pve!provider", "secret"))
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines,
        endpoint=Endpoint(vip="203.0.113.10", advertised_address="203.0.113.10"),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
    )

    for call in calls:
        patch_names = [Path(p).name for p in call["patches"]]
        assert any("-network.yaml" in name for name in patch_names)
