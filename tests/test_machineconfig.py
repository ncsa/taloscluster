"""Tests for taloscluster.talos.machineconfig: the per-node patch builders and
the ``build_configs`` orchestrator.

No ``talosctl`` binary is needed: ``build_configs`` shells out via
``talosctl.gen_config``, which we monkeypatch to capture its kwargs and return
a sentinel string. The patch builders are pure dict constructors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from taloscluster.config import Secrets
from taloscluster.infrastructure import Endpoint
from taloscluster.talos import machineconfig
from taloscluster.talos.machineconfig import INSTALL_DISK

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
        assert patch["machine"]["install"]["disk"] == INSTALL_DISK
        assert patch["machine"]["install"]["wipe"] is True


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
