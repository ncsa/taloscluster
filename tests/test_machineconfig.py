"""Tests for taloscluster.talos.machineconfig: the per-node patch builders and
the ``build_configs`` orchestrator.

No ``talosctl`` binary is needed: ``build_configs`` shells out via
``talosctl.gen_config``, which we monkeypatch to capture its kwargs and return
a sentinel string. The patch builders are pure dict constructors.

These tests cover only the provider-neutral generator; each backend's
contribution is tested next to that backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from taloscluster.config import ConfigError, Secrets
from taloscluster.infrastructure import Endpoint, TalosContribution, TalosPatch
from taloscluster.talos import machineconfig

DISK = "/dev/vda"
FAKE_DISK = "/dev/xvda"

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

def test_machine_patch_has_no_provider_networking(cfg, ep):
    """Networking is a provider contribution; the shared patch never sets it."""
    for m in cfg.machines.values():
        patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK)
        assert "network" not in patch["machine"]
        assert "pods" not in patch["machine"]


def test_machine_patch_certsans_contains_fip(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK)
    assert FIP in patch["machine"]["certSANs"]


def test_machine_patch_install_image_is_installer_ref(cfg, ep):
    for host in cfg.machines:
        m = cfg.machines[host]
        patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK)
        assert patch["machine"]["install"]["image"] == INSTALLER
        assert patch["machine"]["install"]["wipe"] is True


def test_machine_patch_install_disk_comes_from_contribution(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, FAKE_DISK)
    assert patch["machine"]["install"]["disk"] == FAKE_DISK


def test_machine_patch_nodelabels_carry_role_and_pool(cfg, ep):
    cp = cfg.machines["testcluster-controlplane-01"]
    wk = cfg.machines["testcluster-worker-01"]
    cp_patch = machineconfig._machine_patch(cp, cfg, ep, INSTALLER, DISK)
    wk_patch = machineconfig._machine_patch(wk, cfg, ep, INSTALLER, DISK)
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
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK,
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
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK,
                                         default_tags={"ncsa/project": "bbdb"})
    assert patch["machine"]["nodeLabels"]["ncsa/project"] == "override"


def test_machine_patch_kubelet_node_ip_pinned_to_cidr(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK)
    assert patch["machine"]["kubelet"]["nodeIP"]["validSubnets"] == [cfg.cidr]
    assert patch["machine"]["kubelet"]["extraArgs"]["rotate-server-certificates"] is True


def test_machine_patch_time_servers_from_cfg(cfg, ep):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, ep, INSTALLER, DISK)
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


def _contributions(cfg, *patches, install_disk=DISK):
    """A fake third provider's contribution for every machine."""
    return {
        host: TalosContribution(install_disk=install_disk, patches=tuple(patches))
        for host in cfg.machines
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
        contributions=_contributions(cfg),
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
        contributions=_contributions(cfg),
    )

    for call, (_host, m) in zip(calls, cfg.machines.items(), strict=True):
        expected = "controlplane" if m.role == "controlplane" else "worker"
        assert call["output_type"] == expected
        assert call["install_disk"] == DISK


def test_build_configs_passes_contribution_disk_to_talosctl(cfg, monkeypatch, tmp_path, ep):
    """A provider chooses its own install disk without touching this module."""
    calls = []

    def fake_gen_config(**kwargs):
        # read the patch before the temporary workdir is cleaned up
        kwargs["machine_patch"] = _yaml.safe_load(Path(kwargs["patches"][0]).read_text())
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets = Secrets(openstack_credential_id="id", openstack_credential_secret="secret")
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=ep, secrets_path=secrets_path,
        installer_images=_installer_images(cfg),
        contributions=_contributions(cfg, install_disk=FAKE_DISK),
    )

    assert calls
    assert all(call["install_disk"] == FAKE_DISK for call in calls)
    assert all(
        call["machine_patch"]["machine"]["install"]["disk"] == FAKE_DISK for call in calls
    )


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
        contributions=_contributions(cfg),
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
        contributions=_contributions(cfg),
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
        contributions=_contributions(cfg),
    )

    for call, (host, m) in zip(calls, cfg.machines.items(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        if m.role == "controlplane":
            assert f"{host}-cluster.yaml" in patch_names
        else:
            assert f"{host}-cluster.yaml" not in patch_names


# ---------------------------------------------------------------------------
# provider contributions
# ---------------------------------------------------------------------------

def _capture(monkeypatch):
    calls = []

    def fake_gen_config(**kwargs):
        kwargs["documents"] = [
            list(_yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    return calls


def _build(cfg, tmp_path, contributions, **kwargs):
    secrets = Secrets(openstack_credential_id="id", openstack_credential_secret="secret")
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")
    return machineconfig.build_configs(
        cfg, secrets, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
        contributions=contributions, **kwargs,
    )


def test_generator_is_provider_neutral():
    """No provider config types, no provider modules, no provider-name branches."""
    for value in vars(machineconfig).values():
        module = getattr(value, "__module__", "") or ""
        assert not module.startswith(("taloscluster.proxmox", "taloscluster.openstack"))
    source = Path(machineconfig.__file__).read_text()
    for token in ("ProxmoxConfig", "OpenStackConfig", "provider_name", "cfg.provider"):
        assert token not in source


def test_contribution_injects_named_talos_resource(cfg, monkeypatch, tmp_path):
    """A fake third provider adds a Talos resource document without generator edits."""
    calls = _capture(monkeypatch)
    resource = {"apiVersion": "v1alpha1", "kind": "FakeProviderConfig", "name": "fake"}

    _build(cfg, tmp_path, _contributions(cfg, TalosPatch("fake-net", [resource])))

    for call in calls:
        names = [Path(p).name for p in call["patches"]]
        assert any(name.endswith("-fake-net.yaml") for name in names)
        assert resource in [doc for docs in call["documents"] for doc in docs]


def test_contribution_patches_come_before_user_patches(make_config, monkeypatch, tmp_path):
    cfg = make_config({
        "talos": {"config_patches": ["machine:\n  install:\n    disk: /dev/user\n"]},
    })
    calls = _capture(monkeypatch)

    _build(cfg, tmp_path, _contributions(cfg, TalosPatch("provider", {"machine": {}})))

    for call in calls:
        names = [Path(p).name for p in call["patches"]]
        provider_at = next(i for i, n in enumerate(names) if n.endswith("-provider.yaml"))
        user_at = next(i for i, n in enumerate(names) if "-extra-" in n)
        assert provider_at < user_at
        assert user_at == len(names) - 1


def test_patch_order_is_deterministic(cfg, monkeypatch, tmp_path):
    calls = _capture(monkeypatch)
    contributions = _contributions(cfg, TalosPatch("a", {"machine": {}}),
                                   TalosPatch("b", {"machine": {}}))

    _build(cfg, tmp_path, contributions)
    first = [[Path(p).name for p in call["patches"]] for call in calls]
    calls.clear()
    _build(cfg, tmp_path, contributions)
    second = [[Path(p).name for p in call["patches"]] for call in calls]

    assert first == second
    cp = first[0]
    assert [n.split("-controlplane-01-")[-1] for n in cp] == [
        "machine.yaml", "hostname.yaml", "cluster.yaml", "a.yaml", "b.yaml",
    ]


def test_build_configs_requires_a_contribution_per_machine(cfg, monkeypatch, tmp_path):
    _capture(monkeypatch)
    contributions = _contributions(cfg)
    contributions.pop("testcluster-worker-01")

    with pytest.raises(ConfigError, match="testcluster-worker-01"):
        _build(cfg, tmp_path, contributions)


@pytest.mark.parametrize("name", [
    "../escape", "sub/dir", "/absolute", "with space", "Upper", "", "-lead", "trail-",
])
def test_contribution_patch_name_must_be_a_plain_identifier(cfg, monkeypatch, tmp_path, name):
    """A provider is third-party code; its patch name becomes a filename."""
    _capture(monkeypatch)

    with pytest.raises(ConfigError, match="must be lowercase"):
        _build(cfg, tmp_path, _contributions(cfg, TalosPatch(name, {"machine": {}})))


def test_contribution_patch_name_allows_internal_hyphens(cfg, monkeypatch, tmp_path):
    calls = _capture(monkeypatch)

    _build(cfg, tmp_path, _contributions(cfg, TalosPatch("return-path", {"machine": {}})))

    assert all(
        any(Path(p).name.endswith("-return-path.yaml") for p in call["patches"])
        for call in calls
    )
