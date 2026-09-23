"""Tests for taloscluster.talos.machineconfig: the per-node patch builders and
the ``build_configs`` orchestrator.

No ``talosctl`` binary is needed: ``build_configs`` shells out via
``talosctl.gen_config``, which we monkeypatch to capture its kwargs and return
a sentinel string. The patch builders are pure dict constructors.

These tests cover only the provider-neutral generator; each backend's
contribution is tested next to that backend.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml as _yaml

from taloscluster.config import ConfigError, L2Network
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
def cfg_typed(make_config):
    """The same cluster pinned to a Talos minor whose generated config carries
    the typed document layout instead of the v1alpha1 fields (at a kubernetes
    version the pinned Talos runs)."""
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "tailscale": {"login_server": "https://headscale.example.com"},
        "talos": {"version": "v1.14.0"},
        "kubernetes": {"version": "v1.33.0"},
    })


@pytest.fixture
def cfg_with_key(make_config):
    """The same cluster with a tailscale pre-auth key configured."""
    return make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 1, "flavor": "gp.xlarge", "disk": 50}},
        "tailscale": {
            "login_server": "https://headscale.example.com",
            "auth_key": "tskey-secret",
        },
    })


@pytest.fixture
def ep() -> Endpoint:
    return Endpoint(vip=VIP, advertised_address=FIP)


# ---------------------------------------------------------------------------
# typed_documents
# ---------------------------------------------------------------------------

def test_typed_documents_follows_the_talos_minor():
    assert machineconfig.typed_documents("v1.13.9") is False
    assert machineconfig.typed_documents("v1.14.0") is True
    assert machineconfig.typed_documents("v1.15.1") is True
    assert machineconfig.typed_documents("nonsense") is False


# ---------------------------------------------------------------------------
# _machine_patch (v1alpha1 layout, Talos up to 1.13)
# ---------------------------------------------------------------------------

def test_machine_patch_has_no_provider_networking(cfg):
    """Networking is a provider contribution; the shared patch never sets it."""
    for m in cfg.machines.values():
        patch = machineconfig._machine_patch(m, cfg, DISK)
        assert "network" not in patch["machine"]
        assert "pods" not in patch["machine"]


def test_machine_patch_carries_no_certsans(cfg):
    """The endpoint SAN rides `--additional-sans` on gen config, which fills
    the machine and API-server certSANs on every supported version."""
    for m in cfg.machines.values():
        patch = machineconfig._machine_patch(m, cfg, DISK)
        assert "certSANs" not in patch["machine"]


def test_machine_patch_install_adds_only_the_wipe(cfg):
    """`--install-disk`/`--install-image` fill the disk and image on gen
    config; the patch only forces the wipe."""
    for m in cfg.machines.values():
        patch = machineconfig._machine_patch(m, cfg, DISK)
        assert patch["machine"]["install"] == {"wipe": True}


def test_machine_patch_nodelabels_carry_role_and_pool(cfg):
    cp = cfg.machines["testcluster-controlplane-01"]
    wk = cfg.machines["testcluster-worker-01"]
    cp_patch = machineconfig._machine_patch(cp, cfg, DISK)
    wk_patch = machineconfig._machine_patch(wk, cfg, DISK)
    assert cp_patch["machine"]["nodeLabels"] == {
        "ncsa/role": "controlplane", "ncsa/pool": "controlplane"
    }
    assert wk_patch["machine"]["nodeLabels"] == {"ncsa/role": "worker", "ncsa/pool": "worker"}


def test_machine_patch_nodelabels_include_tags_and_defaults(make_config):
    cfg = make_config({
        "tags": {"team": "platform"},
        "workers": {"worker": {
            "count": 1, "flavor": "gp.xlarge", "disk": 50,
            "tags": {"workload": "batch"},
        }},
    })
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK,
                                         default_tags={"ncsa/project": "my project"})
    assert patch["machine"]["nodeLabels"] == {
        "ncsa/role": "worker",
        "ncsa/pool": "worker",
        "ncsa/project": "my_project",  # spaces in the project name become _
        "team": "platform",
        "workload": "batch",
    }


def test_machine_patch_user_tag_overrides_default(make_config):
    cfg = make_config({"tags": {"ncsa/project": "override"}})
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK,
                                         default_tags={"ncsa/project": "bbdb"})
    assert patch["machine"]["nodeLabels"]["ncsa/project"] == "override"


def test_machine_patch_kubelet_node_ip_pinned_to_cidr(cfg):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK)
    assert patch["machine"]["kubelet"]["nodeIP"]["validSubnets"] == [cfg.network.cluster.cidr]
    assert patch["machine"]["kubelet"]["extraArgs"]["rotate-server-certificates"] is True


def test_machine_patch_node_cidr_overrides_the_cluster_cidr(cfg):
    """A node off the cluster network (a metal server) pins the pod node IP to
    its own L2."""
    m = cfg.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK, node_cidr="203.0.113.0/24")
    assert patch["machine"]["kubelet"]["nodeIP"]["validSubnets"] == ["203.0.113.0/24"]


def test_machine_patch_time_servers_from_cfg(cfg):
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK)
    assert patch["machine"]["time"]["servers"] == cfg.network.ntp


# ---------------------------------------------------------------------------
# _machine_patch (typed document layout, Talos 1.14+)
# ---------------------------------------------------------------------------

def test_typed_machine_patch_moves_the_fields_into_the_typed_documents(cfg_typed):
    """The settings 1.14 moved out of v1alpha1 ride the typed documents that
    own them -- patching the v1alpha1 homes there is rejected as already set.
    The kubelet arg is a string on the typed KubeletConfig, and the install
    patch must restate the disk selector the UnattendedInstallConfig merge
    would otherwise drop."""
    m = cfg_typed.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg_typed, DISK)
    assert patch == [
        {
            "apiVersion": "v1alpha1",
            "kind": "KubeNodeConfig",
            "labels": {"ncsa/role": "controlplane", "ncsa/pool": "controlplane"},
            "nodeIP": {"validSubnets": [cfg_typed.network.cluster.cidr]},
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "KubeletConfig",
            "extraArgs": {"rotate-server-certificates": "true"},
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "UnattendedInstallConfig",
            "provisioning": {
                "diskSelector": {"match": f'disk.dev_path == "{DISK}"'},
                "wipe": True,
            },
        },
        {"machine": {"time": {"servers": cfg_typed.network.ntp}}},
    ]


def test_typed_machine_patch_node_cidr_overrides_the_cluster_cidr(cfg_typed):
    m = cfg_typed.machines["testcluster-worker-01"]
    patch = machineconfig._machine_patch(m, cfg_typed, DISK, node_cidr="203.0.113.0/24")
    (kubenode,) = [d for d in patch if d.get("kind") == "KubeNodeConfig"]
    assert kubenode["nodeIP"]["validSubnets"] == ["203.0.113.0/24"]


def test_machine_patch_layout_follows_the_version_argument(cfg, cfg_typed):
    """The layout comes from the version the config is generated for -- the
    node's RUNNING one during a rollout -- not from cluster.yaml alone."""
    # the config pins 1.14, but the node still runs 1.13: v1alpha1 layout
    m = cfg_typed.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg_typed, DISK, talos_version="v1.13.9")
    assert patch["machine"]["nodeLabels"] == {
        "ncsa/role": "controlplane", "ncsa/pool": "controlplane"
    }
    # and the reverse: a 1.13 cluster.yaml generating for a 1.14 node
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._machine_patch(m, cfg, DISK, talos_version="v1.14.0")
    assert [d.get("kind") for d in patch if isinstance(d, dict)] == [
        "KubeNodeConfig", "KubeletConfig", "UnattendedInstallConfig", None,
    ]


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
# _cluster_patch
# ---------------------------------------------------------------------------

def test_cluster_patch_schedules_off_the_control_planes(cfg):
    """The v1alpha1 layout keeps the flag that keeps pods off the control
    planes; the API-server certSANs are no patch (they ride --additional-sans)."""
    patch = machineconfig._cluster_patch(cfg)
    assert patch["cluster"]["allowSchedulingOnControlPlanes"] is False
    assert "apiServer" not in patch["cluster"]
    assert "certSANs" not in patch["cluster"]
    assert patch["cluster"]["etcd"]["advertisedSubnets"] == [cfg.network.cluster.cidr]


def test_typed_cluster_patch_drops_allow_scheduling(cfg_typed):
    """1.14's generated KubeNodeConfig already taints control planes
    NoSchedule, and the v1alpha1 flag is refused there as already set."""
    patch = machineconfig._cluster_patch(cfg_typed)
    assert "allowSchedulingOnControlPlanes" not in patch["cluster"]
    assert patch["cluster"]["inlineManifests"] == machineconfig.EXTRA_MANIFESTS
    assert patch["cluster"]["etcd"]["advertisedSubnets"] == [cfg_typed.network.cluster.cidr]


def test_cluster_patch_node_cidr_keys_the_etcd_advertisement(cfg):
    patch = machineconfig._cluster_patch(cfg, node_cidr="203.0.113.0/24")
    assert patch["cluster"]["etcd"]["advertisedSubnets"] == ["203.0.113.0/24"]


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


def test_tailscale_patch_no_login_server(make_config):
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
        "tailscale": {},
    })
    m = cfg.machines["testcluster-controlplane-01"]
    patch = machineconfig._tailscale_patch(m, cfg, "tskey-secret")
    env = patch["environment"]
    assert "TS_AUTHKEY=tskey-secret" in env
    assert f"TS_HOSTNAME={m.name}" in env
    extra = [line for line in env if line.startswith("TS_EXTRA_ARGS=")]
    assert len(extra) == 1
    assert extra[0] == "TS_EXTRA_ARGS="


# ---------------------------------------------------------------------------
# _kubespan_patch
# ---------------------------------------------------------------------------

def test_kubespan_patch_defaults_to_the_l2_mtu_minus_overhead(make_config):
    patch = machineconfig._kubespan_patch(make_config())
    assert patch == {
        "machine": {"network": {"kubespan": {"enabled": True, "mtu": 1420}}}
    }


def test_kubespan_patch_excludes_the_tailnet_when_tailscale_is_on(cfg):
    """A tailscale cluster's nodes own a 100.64/10 address; KubeSpan must never
    advertise or pick one as a peer endpoint, which would tunnel WireGuard in
    WireGuard."""
    patch = machineconfig._kubespan_patch(cfg)
    assert patch["machine"]["network"]["kubespan"]["filters"]["endpoints"] == [
        "0.0.0.0/0", "!100.64.0.0/10",
    ]


def test_kubespan_patch_no_tailnet_filter_without_a_tailscale_section(make_config):
    patch = machineconfig._kubespan_patch(make_config())
    assert "filters" not in patch["machine"]["network"]["kubespan"]


def test_kubespan_patch_uses_the_routed_mtu_when_peers_span_l2s(make_config):
    """A metal group on another L2: cross-L2 packets leave through the gateway
    route clamped to 1500, so the WireGuard MTU follows the routed path even on
    a jumbo cluster L2."""
    cfg = make_config({
        "network": {"cluster": {"mtu": 9000}},
        "talos": {"kubespan": True},
        "metal": {
            "rack": {
                "role": "worker",
                "disk": "/dev/sda",
                "network": {"cidr": "192.168.16.0/24", "gateway": "192.168.16.1"},
                "servers": {
                    "srv01": {
                        "interfaces": {
                            "enp1s0f0": {"role": "cluster", "ip": "192.168.16.5/24"}
                        },
                    },
                },
            },
        },
    })
    patch = machineconfig._kubespan_patch(cfg)
    assert patch["machine"]["network"]["kubespan"]["mtu"] == 1420
    # the same clamp applies to a metal node whose own L2 is jumbo
    server = cfg.metal.groups["rack"].servers["srv01"]
    own = machineconfig._kubespan_patch(cfg, mtu=server.network.mtu)
    assert own["machine"]["network"]["kubespan"]["mtu"] == 1420


def test_kubespan_patch_jumbo_mtu_and_external_filters(make_config):
    """On a jumbo L2 the KubeSpan MTU follows the L2 minus the WireGuard
    overhead, and the external network is excluded from endpoint discovery:
    the filter allows every address and removes the external CIDRs, so a
    node's cluster-L2 address stays advertised as a peer endpoint."""
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {
                "cluster": {"mtu": 9000},
                "external": {
                    "cidr": "203.0.113.0/24",
                    "gateway": "203.0.113.1",
                    "anchor_cidr": "169.254.40.0/24",
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
    patch = machineconfig._kubespan_patch(cfg)
    assert patch == {
        "machine": {"network": {"kubespan": {
            "enabled": True,
            "mtu": 8920,
            "filters": {"endpoints": [
                "0.0.0.0/0", "!203.0.113.0/24", "!169.254.40.0/24",
            ]},
        }}}
    }
    # Talos reads `filters.endpoints` as an allow-list: the positive CIDR
    # advertises, every other entry must remove with a `!` prefix
    endpoints = patch["machine"]["network"]["kubespan"]["filters"]["endpoints"]
    assert endpoints[0] == "0.0.0.0/0"
    assert all(entry.startswith("!") for entry in endpoints[1:])


def test_kubespan_patch_external_without_anchor_omits_the_anchor_filter(make_config):
    """An external network without an anchor CIDR adds no anchor exclusion."""
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {
                "external": {
                    "cidr": "203.0.113.0/24",
                    "gateway": "203.0.113.1",
                    "anchor_cidr": "169.254.40.0/24",
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
    # the loader requires anchor_cidr on an external block; replace it to
    # reach the skip branch
    cfg.network = replace(
        cfg.network, external=L2Network(cidr="203.0.113.0/24", gateway="203.0.113.1")
    )
    patch = machineconfig._kubespan_patch(cfg)
    assert patch["machine"]["network"]["kubespan"]["filters"]["endpoints"] == [
        "0.0.0.0/0", "!203.0.113.0/24",
    ]


# ---------------------------------------------------------------------------
# node_patches: the shared stack both generators ride
# ---------------------------------------------------------------------------

def test_node_patches_stacks_the_shared_patches_in_order(cfg_with_key, tmp_path):
    """machine, hostname, encryption, cluster, firewall, kubespan, tailscale --
    one file per patch, in the order gen config takes them, for a control
    plane whose extensions carry tailscale."""
    cfg = replace(cfg_with_key, kubespan=True)
    m = cfg.machines["testcluster-controlplane-01"]
    paths = machineconfig.node_patches(
        tmp_path, "node-01", m, cfg, DISK, passphrase="luks-passphrase",
    )

    assert [p.name for p in paths] == [
        "node-01-machine.yaml", "node-01-hostname.yaml", "node-01-encryption.yaml",
        "node-01-cluster.yaml", "node-01-firewall.yaml", "node-01-kubespan.yaml",
        "node-01-tailscale.yaml",
    ]
    assert all(p.exists() for p in paths)


def test_node_patches_omits_the_conditional_patches(cfg, tmp_path):
    """No passphrase, worker role, no kubespan opt-in, no auth key: only the
    machine, hostname and firewall patches are written."""
    m = cfg.machines["testcluster-worker-01"]
    paths = machineconfig.node_patches(tmp_path, "worker-01", m, cfg, DISK)

    assert [p.name for p in paths] == [
        "worker-01-machine.yaml", "worker-01-hostname.yaml", "worker-01-firewall.yaml",
    ]


def test_node_patches_keys_the_stack_on_an_off_cluster_l2(cfg, tmp_path):
    """`node_cidr` and `kubespan_mtu` steer the machine, cluster, firewall and
    kubespan patches at a node sitting off the cluster network -- a metal
    server's own L2."""
    cfg = replace(cfg, kubespan=True)
    m = cfg.machines["testcluster-controlplane-01"]
    paths = machineconfig.node_patches(
        tmp_path, "node-01", m, cfg, DISK,
        node_cidr="203.0.113.0/24", kubespan_mtu=9000,
    )
    by_name = {p.name: list(_yaml.safe_load_all(p.read_text())) for p in paths}

    (machine,) = by_name["node-01-machine.yaml"]
    assert machine["machine"]["kubelet"]["nodeIP"]["validSubnets"] == ["203.0.113.0/24"]
    (cluster,) = by_name["node-01-cluster.yaml"]
    assert cluster["cluster"]["etcd"]["advertisedSubnets"] == ["203.0.113.0/24"]
    (kubespan,) = by_name["node-01-kubespan.yaml"]
    assert kubespan["machine"]["network"]["kubespan"]["mtu"] == 8920
    cluster_tcp = next(
        doc for doc in by_name["node-01-firewall.yaml"]
        if doc.get("name") == "cluster-tcp"
    )
    assert {"subnet": "203.0.113.0/24"} in cluster_tcp["ingress"]


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

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    configs = machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
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

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
        contributions=_contributions(cfg),
    )

    for call, (_host, m) in zip(calls, cfg.machines.items(), strict=True):
        expected = "controlplane" if m.role == "controlplane" else "worker"
        assert call["output_type"] == expected
        assert call["install_disk"] == DISK


def test_build_configs_passes_contribution_disk_to_talosctl(cfg, monkeypatch, tmp_path, ep):
    """A provider chooses its own install disk without touching this module:
    it rides gen config's --install-disk (and the typed layout's UnattendedInstallConfig)."""
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=ep, secrets_path=secrets_path,
        installer_images=_installer_images(cfg),
        contributions=_contributions(cfg, install_disk=FAKE_DISK),
    )

    assert calls
    assert all(call["install_disk"] == FAKE_DISK for call in calls)


def test_build_configs_passes_the_advertised_address_as_additional_sans(
    cfg, monkeypatch, tmp_path, ep
):
    """The endpoint SAN is a gen config flag, not a patch: it fills the machine
    certSANs and the API server's on every supported layout."""
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=ep, secrets_path=secrets_path,
        installer_images=_installer_images(cfg),
        contributions=_contributions(cfg),
    )

    assert calls
    assert all(call["additional_sans"] == [FIP] for call in calls)


def test_build_configs_generates_the_running_layout_per_host(cfg_typed, monkeypatch, tmp_path):
    """Each node's config is generated at the Talos version it RUNS: a node
    still on 1.13 gets the v1alpha1 layout while a 1.14 node gets the typed
    documents, and gen config's --talos-version matches the layout."""
    calls = []

    def fake_gen_config(**kwargs):
        kwargs["documents"] = [
            list(_yaml.safe_load_all(Path(p).read_text())) for p in kwargs["patches"]
        ]
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    hosts = list(cfg_typed.machines)
    assert len(hosts) == 2
    machineconfig.build_configs(
        cfg_typed, cfg_typed.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg_typed),
        contributions=_contributions(cfg_typed),
        talos_versions={hosts[0]: "v1.13.9"},  # hosts[1] absent -> the target
    )

    by_host = {Path(c["patches"][0]).name.removesuffix("-machine.yaml"): c for c in calls}
    old, new = by_host[hosts[0]], by_host[hosts[1]]
    assert old["talos_version"] == "v1.13.9"
    assert new["talos_version"] == "v1.14.0"
    (old_patch,) = old["documents"][0]
    assert "nodeLabels" in old_patch["machine"]  # v1alpha1 layout
    assert [d.get("kind") for d in new["documents"][0] if isinstance(d, dict)] == [
        "KubeNodeConfig", "KubeletConfig", "UnattendedInstallConfig", None,
    ]


def test_build_configs_tailscale_patch_present_when_key_set(
    cfg_with_key, monkeypatch, tmp_path
):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg_with_key, cfg_with_key.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg_with_key),
        contributions=_contributions(cfg_with_key),
    )

    for call, host in zip(calls, cfg_with_key.machines.keys(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        assert f"{host}-tailscale.yaml" in patch_names


def test_build_configs_no_tailscale_patch_when_key_absent(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
        contributions=_contributions(cfg),
    )

    for call, host in zip(calls, cfg.machines.keys(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        assert f"{host}-tailscale.yaml" not in patch_names


def test_build_configs_stacks_the_kubespan_patch_on_every_node(
    cfg, monkeypatch, tmp_path
):
    cfg = replace(cfg, kubespan=True)  # the patch rides an explicit opt-in
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
        contributions=_contributions(cfg),
    )

    for call, host in zip(calls, cfg.machines.keys(), strict=True):
        patch_names = [Path(p).name for p in call["patches"]]
        assert f"{host}-kubespan.yaml" in patch_names


def test_build_configs_no_kubespan_patch_by_default(
    make_config, monkeypatch, tmp_path
):
    cfg = make_config()
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
        secrets_path=secrets_path, installer_images=_installer_images(cfg),
        contributions=_contributions(cfg),
    )

    for call in calls:
        patch_names = [Path(p).name for p in call["patches"]]
        assert not any(name.endswith("-kubespan.yaml") for name in patch_names)


def test_build_configs_cluster_patch_only_for_controlplane(cfg, monkeypatch, tmp_path):
    calls = []

    def fake_gen_config(**kwargs):
        calls.append(kwargs)
        return "CONFIG"

    monkeypatch.setattr(machineconfig.talosctl, "gen_config", fake_gen_config)

    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
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
# system disk encryption + vendored bootstrap manifests
# ---------------------------------------------------------------------------

def _secrets_with_passphrase(tmp_path: Path, passphrase: str = "luks-passphrase-0123") -> Path:
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text(
        f"cluster:\n  id: abc\n  secret: def\n"
        f"{machineconfig.DISK_PASSPHRASE_KEY}: {passphrase}\n"
    )
    return secrets_path


def _encryption_patches(calls):
    """The systemDiskEncryption patch documents, in gen_config call order."""
    found = []
    for call in calls:
        for docs in call["documents"]:
            for doc in docs:
                if isinstance(doc, dict):
                    encryption = (doc.get("machine") or {}).get("systemDiskEncryption")
                    if encryption:
                        found.append(encryption)
    return found


def test_build_configs_emits_disk_encryption_when_secrets_carry_the_passphrase(
    cfg, monkeypatch, tmp_path
):
    calls = _capture(monkeypatch)
    secrets_path = _secrets_with_passphrase(tmp_path)

    _build(cfg, tmp_path, _contributions(cfg), secrets_path=secrets_path)

    patches = _encryption_patches(calls)
    assert len(patches) == len(cfg.machines)
    for encryption in patches:
        assert encryption["state"]["provider"] == "luks2"
        assert encryption["ephemeral"]["provider"] == "luks2"
        for partition in ("state", "ephemeral"):
            (key,) = encryption[partition]["keys"]
            assert key["slot"] == 0
            assert key["static"]["passphrase"] == "luks-passphrase-0123"


def test_build_configs_omits_disk_encryption_without_the_passphrase(
    cfg, monkeypatch, tmp_path
):
    """A secrets file without the key is a cluster created before system disk
    encryption: its machines were installed unencrypted, so no config may
    carry the settings."""
    calls = _capture(monkeypatch)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("cluster:\n  id: abc\n  secret: def\n")

    _build(cfg, tmp_path, _contributions(cfg), secrets_path=secrets_path)

    assert _encryption_patches(calls) == []


def test_build_configs_cluster_patch_vendors_the_bootstrap_manifests(
    cfg, monkeypatch, tmp_path
):
    """The approver and metrics-server manifests ride inlineManifests on control
    planes -- pinned in this repo, not fetched from GitHub by a movable URL."""
    calls = _capture(monkeypatch)
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text("dummy")

    _build(cfg, tmp_path, _contributions(cfg), secrets_path=secrets_path)

    for call, (_host, m) in zip(calls, cfg.machines.items(), strict=True):
        manifests = None
        for docs in call["documents"]:
            for doc in docs:
                if isinstance(doc, dict) and "cluster" in doc:
                    manifests = doc["cluster"].get("inlineManifests")
        if m.role == "controlplane":
            assert manifests is not None
            assert {entry["name"] for entry in manifests} == {
                "kubelet-serving-cert-approver", "metrics-server",
            }
            for entry in manifests:
                docs = list(_yaml.safe_load_all(entry["contents"]))
                assert docs and all(isinstance(doc, dict) for doc in docs)
        else:
            assert manifests is None
    # the image tags the vendored manifests bake are pinned here too, so a
    # bump to either vendored copy shows up in this test's diff
    assert "kubelet-serving-cert-approver:0.11.0" in machineconfig.CERT_APPROVER_MANIFEST
    assert "metrics-server:v0.9.0" in machineconfig.METRICS_SERVER_MANIFEST


def test_disk_passphrase_reads_only_a_string_key(tmp_path):
    secrets_path = tmp_path / "talossecrets.yaml"
    secrets_path.write_text(
        f"{machineconfig.DISK_PASSPHRASE_KEY}: some-passphrase\n"
    )
    assert machineconfig.disk_passphrase(secrets_path) == "some-passphrase"
    secrets_path.write_text("dummy")
    assert machineconfig.disk_passphrase(secrets_path) is None
    secrets_path.write_text(f"{machineconfig.DISK_PASSPHRASE_KEY}: 12345\n")
    assert machineconfig.disk_passphrase(secrets_path) is None


def test_with_disk_passphrase_appends_the_key():
    raw = "cluster:\n  id: abc\n  secret: def\n"
    combined = machineconfig.with_disk_passphrase(raw)
    data = _yaml.safe_load(combined)
    assert data["cluster"] == {"id": "abc", "secret": "def"}
    passphrase = data[machineconfig.DISK_PASSPHRASE_KEY]
    assert isinstance(passphrase, str) and len(passphrase) >= 32
    # a second bundle gets a different passphrase
    assert machineconfig.with_disk_passphrase(raw) != combined


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


def _build(cfg, tmp_path, contributions, *, secrets_path=None, **kwargs):
    if secrets_path is None:
        secrets_path = tmp_path / "talossecrets.yaml"
        secrets_path.write_text("dummy")
    return machineconfig.build_configs(
        cfg, cfg.machines, endpoint=Endpoint(vip=VIP, advertised_address=FIP),
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
    cfg = replace(cfg, kubespan=True)  # the kubespan slot sits mid-stack
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
        "machine.yaml", "hostname.yaml", "cluster.yaml", "firewall.yaml",
        "kubespan.yaml", "a.yaml", "b.yaml",
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


# ---------------------------------------------------------------------------
# return-path pod version override
# ---------------------------------------------------------------------------

def _return_path_contribution(cfg, image):
    pod = {
        "metadata": {"name": "taloscluster-proxmox-return-path"},
        "spec": {"containers": [{"name": "return-path", "image": image}]},
    }
    return _contributions(cfg, TalosPatch("return-path", {"machine": {"pods": [pod]}}))


def _pod_image(calls):
    images = []
    for call in calls:
        for docs in call["documents"]:
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                for pod in (doc.get("machine") or {}).get("pods") or []:
                    images.append(pod["spec"]["containers"][0]["image"])
    return images


def test_build_configs_retags_return_path_pod_to_running_version(cfg, monkeypatch, tmp_path):
    """On an upgrade build_configs bakes the RUNNING version into configs, so the
    return-path pod -- which the provider built with the (newer) target kube-proxy
    -- must be retagged to the running version, or every node would pull the target
    image at apply time, before the minor-by-minor upgrade."""
    calls = _capture(monkeypatch)
    image = f"registry.k8s.io/kube-proxy:{cfg.kubernetes_version}"
    contributions = _return_path_contribution(cfg, image)

    _build(cfg, tmp_path, contributions, kubernetes_version="v1.33.4")

    assert set(_pod_image(calls)) == {"registry.k8s.io/kube-proxy:v1.33.4"}


def test_build_configs_keeps_target_pod_when_version_matches(cfg, monkeypatch, tmp_path):
    """A fresh cluster or new-node rebuild bakes the target version: the pod image
    (also target) is left untouched."""
    calls = _capture(monkeypatch)
    image = f"registry.k8s.io/kube-proxy:{cfg.kubernetes_version}"
    contributions = _return_path_contribution(cfg, image)

    _build(cfg, tmp_path, contributions)  # no override

    assert set(_pod_image(calls)) == {image}
