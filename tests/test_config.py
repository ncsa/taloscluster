"""Tests for taloscluster.config: machines expansion, extension/patch resolution,
validation errors, warnings, and cached_property semantics."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster import naming
from taloscluster.config import (
    SECRETS_FILE,
    ConfigError,
    L2Network,
    MetalBmc,
    MetalConfig,
    MetalGroup,
    MetalInterface,
    MetalServer,
    OpenStackConfig,
    ProxmoxConfig,
    SecurityRule,
    load_config,
    proxmox_sdn,
    validate_warnings,
)
from taloscluster.naming import BASE_EXTENSIONS

# ---------------------------------------------------------------------------
# machines expansion
# ---------------------------------------------------------------------------

def test_machines_expansion_controlplane_and_worker(make_config):
    cfg = make_config({
        "controlplane": {"count": 3, "flavor": "gp.medium", "disk": 40},
        "workers": {"worker": {"count": 2, "flavor": "gp.xlarge", "disk": 50}},
    })
    machines = cfg.machines
    assert len(machines) == 5
    # controlplanes
    for i in range(1, 4):
        host = f"testcluster-controlplane-{i:02d}"
        m = machines[host]
        assert m.name == host
        assert m.role == "controlplane"
        assert m.pool == "controlplane"
        assert m.flavor == "gp.medium"
        assert m.disk == 40
    # workers
    for i in range(1, 3):
        host = f"testcluster-worker-{i:02d}"
        m = machines[host]
        assert m.name == host
        assert m.role == "worker"
        assert m.pool == "worker"
        assert m.flavor == "gp.xlarge"
        assert m.disk == 50


def test_machines_hostnames_are_zero_padded(make_config):
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 20},
        "workers": {"gpu": {"count": 1, "flavor": "g", "disk": 100}},
    })
    keys = list(cfg.machines)
    assert "testcluster-controlplane-01" in keys
    assert "testcluster-gpu-01" in keys


def test_hostname_length_uses_the_widest_real_ordinal(make_config):
    # name + pool = 59 chars; -NN = 63 chars fits, so this used to pass. At 100
    # nodes the ordinal is 3 digits (-100, 64 chars), which must now be rejected.
    pool = "p" * 48
    cfg = make_config({
        "controlplane": {"count": 1, "flavor": "f", "disk": 20},
        "workers": {pool: {"count": 99, "flavor": "f", "disk": 20}},
    })
    assert f"testcluster-{pool}-99" in cfg.machines  # 63 chars is still allowed

    with pytest.raises(ConfigError, match="hostname longer than 63"):
        make_config({
            "controlplane": {"count": 1, "flavor": "f", "disk": 20},
            "workers": {pool: {"count": 100, "flavor": "f", "disk": 20}},
        })


# ---------------------------------------------------------------------------
# extension resolution
# ---------------------------------------------------------------------------

def test_extensions_base_always_present(make_config):
    cfg = make_config({"tailscale": {"login_server": "https://hs.example"}})
    for m in cfg.machines.values():
        assert set(BASE_EXTENSIONS).issubset(set(m.extensions))
        # sorted + deduped tuple
        assert m.extensions == tuple(sorted(set(m.extensions)))


def test_tailscale_extension_dropped_without_tailscale_section(make_config):
    # the boot ISO still bakes it, but the installed system (install.image
    # schematic) omits tailscale when no tailscale: section is configured
    cfg = make_config()
    assert "tailscale" not in cfg.raw
    for m in cfg.machines.values():
        assert "siderolabs/tailscale" not in m.extensions
        assert "siderolabs/qemu-guest-agent" in m.extensions


def test_extensions_cluster_and_pool_merged(make_config):
    cfg = make_config({
        "tailscale": {"login_server": "https://hs.example"},
        "talos": {"extensions": ["siderolabs/nvidia-gpu"]},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 20,
                                "extensions": ["siderolabs/nvidia-gpu", "extra/thing"]}},
    })
    cp = cfg.machines["testcluster-controlplane-01"]
    wk = cfg.machines["testcluster-worker-01"]
    # cluster-level extension reaches controlplane but NOT the pool-only one
    assert "siderolabs/nvidia-gpu" in cp.extensions
    assert "extra/thing" not in cp.extensions
    # worker gets base + cluster + pool, sorted + deduped
    expected_wk = tuple(sorted(set(BASE_EXTENSIONS) | {"siderolabs/nvidia-gpu", "extra/thing"}))
    assert wk.extensions == expected_wk
    # dedup: nvidia-gpu is in both cluster and pool lists, appears once
    assert wk.extensions.count("siderolabs/nvidia-gpu") == 1


# ---------------------------------------------------------------------------
# config_patches precedence
# ---------------------------------------------------------------------------

def test_config_patches_cluster_first_pool_appended(make_config):
    cfg = make_config({
        "talos": {"config_patches": ["cluster-patch-1", "cluster-patch-2"]},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 20,
                                "config_patches": ["pool-patch-1"]}},
    })
    cp = cfg.machines["testcluster-controlplane-01"]
    wk = cfg.machines["testcluster-worker-01"]
    assert cp.config_patches == ("cluster-patch-1", "cluster-patch-2")
    # pool patches appended AFTER cluster patches
    assert wk.config_patches == ("cluster-patch-1", "cluster-patch-2", "pool-patch-1")


# ---------------------------------------------------------------------------
# extension_sets
# ---------------------------------------------------------------------------

def test_extension_sets_one_per_distinct_tuple(make_config):
    cfg = make_config({
        "talos": {"extensions": ["extra/only-cluster"]},
        "workers": {
            "worker": {"count": 2, "flavor": "f", "disk": 20},
            "gpu": {"count": 1, "flavor": "g", "disk": 100,
                    "extensions": ["siderolabs/nvidia-gpu"]},
        },
    })
    sets = cfg.extension_sets()
    # controlplane and worker share the same extension set (cluster-level only)
    cp_ext = cfg.machines["testcluster-controlplane-01"].extensions
    wk_ext = cfg.machines["testcluster-worker-01"].extensions
    gpu_ext = cfg.machines["testcluster-gpu-01"].extensions
    assert cp_ext == wk_ext
    assert gpu_ext != cp_ext
    # one entry per distinct tuple -> 2 distinct sets
    assert len(sets) == 2
    assert cp_ext in sets
    assert gpu_ext in sets


# ---------------------------------------------------------------------------
# missing required keys -> ConfigError
# ---------------------------------------------------------------------------

def test_missing_name_raises_config_error(make_config):
    with pytest.raises(ConfigError):
        make_config(remove=("name",))


def test_missing_talos_version_raises_config_error(make_config):
    with pytest.raises(ConfigError):
        make_config(remove=("talos.version",))


def test_pool_missing_flavor_raises_config_error(make_config):
    with pytest.raises(ConfigError):
        make_config({"workers": {"worker": {"count": 2, "disk": 40}}})


# ---------------------------------------------------------------------------
# non-integer count -> ConfigError (via _int), not ValueError
# ---------------------------------------------------------------------------

def test_non_integer_count_raises_config_error_not_value_error(make_config):
    with pytest.raises(ConfigError):
        make_config({"controlplane": {"count": "three"}})


def test_non_integer_count_in_worker_pool_raises_config_error(make_config):
    with pytest.raises(ConfigError):
        make_config({"workers": {"worker": {"count": "two", "flavor": "f", "disk": 20}}})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "Bad_Name"}, "name"),
        ({"talos": {"version": "latest"}}, "talos.version"),
        ({"talos": {"version": "v1.12.9"}}, "v1.13.0 or newer"),
        ({"network": {"cluster": {"cidr": "not-a-cidr"}}}, "network.cluster.cidr"),
        ({"controlplane": {"count": 0}}, "controlplane"),
        ({"workers": {"worker": {"count": -1, "flavor": "f", "disk": 20}}}, "count"),
        ({"workers": {"controlplane": {"count": 1, "flavor": "f", "disk": 20}}},
         "reserved"),
        ({"workers": []}, "workers"),
        ({"tags": []}, "tags"),
        ({"talos": {"extensions": {}}}, "talos.extensions"),
        ({"network": {"dns": "1.1.1.1"}}, "network.dns"),
    ],
)
def test_invalid_configuration_fails_during_load(make_config, overrides, message):
    with pytest.raises(ConfigError, match=message):
        make_config(overrides)


def test_unprefixed_talos_version_is_normalized(make_config):
    cfg = make_config({"talos": {"version": "1.13.9"}})
    assert cfg.talos_version == "v1.13.9"


def test_prefixed_talos_version_is_kept(make_config):
    cfg = make_config({"talos": {"version": "v1.14.2"}})
    assert cfg.talos_version == "v1.14.2"


def test_non_string_talos_version_raises_config_error_not_attribute_error(make_config):
    # An unquoted `1.13` parses as a float; normalization must not run before
    # validation or it raises AttributeError instead of ConfigError.
    with pytest.raises(ConfigError):
        make_config({"talos": {"version": 1.13}})


def test_unprefixed_kubernetes_version_is_normalized(make_config):
    cfg = make_config({"kubernetes": {"version": "1.31.0"}})
    assert cfg.kubernetes_version == "v1.31.0"


def test_prefixed_kubernetes_version_is_kept(make_config):
    cfg = make_config({"kubernetes": {"version": "v1.31.0"}})
    assert cfg.kubernetes_version == "v1.31.0"


def test_non_string_kubernetes_version_raises_config_error_not_attribute_error(make_config):
    # An unquoted `1.31` parses as a float; normalization must not run before
    # validation or it raises AttributeError instead of ConfigError.
    with pytest.raises(ConfigError):
        make_config({"kubernetes": {"version": 1.31}})


def test_kubespan_defaults_to_false(make_config):
    assert make_config().kubespan is False


def test_kubespan_can_be_enabled(make_config):
    cfg = make_config({"talos": {"kubespan": True}})
    assert cfg.kubespan is True


def test_non_bool_kubespan_raises_config_error(make_config):
    with pytest.raises(ConfigError, match="talos.kubespan must be true or false"):
        make_config({"talos": {"kubespan": "off"}})


# ---------------------------------------------------------------------------
# validate_warnings
# ---------------------------------------------------------------------------

def test_validate_warnings_even_count(make_config):
    cfg = make_config({"controlplane": {"count": 2, "flavor": "f", "disk": 20}})
    warnings = validate_warnings(cfg)
    assert any("even" in w for w in warnings)
    assert not any("single" in w for w in warnings)


def test_validate_warnings_single_controlplane(make_config):
    cfg = make_config({"controlplane": {"count": 1, "flavor": "f", "disk": 20}})
    warnings = validate_warnings(cfg)
    assert any("single" in w for w in warnings)
    assert not any("even" in w for w in warnings)


def test_validate_warnings_three_no_warnings(make_config):
    cfg = make_config({"controlplane": {"count": 3, "flavor": "f", "disk": 20}})
    assert validate_warnings(cfg) == []


def test_warns_that_dns_is_dhcp_backed_on_proxmox_bridge(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
        },
        remove=("openstack",),
    )
    assert any("DHCP-backed" in w and "network.dns" in w for w in validate_warnings(cfg))


def test_no_dns_warning_on_proxmox_bridge_with_empty_dns(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {"dns": [], "cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
        },
        remove=("openstack",),
    )
    # no configured resolvers, so there is nothing DHCP could be ignoring
    assert all("DHCP-backed" not in w for w in validate_warnings(cfg))


def test_no_dns_warning_on_openstack(make_config):
    cfg = make_config()
    assert all("network.dns" not in w for w in validate_warnings(cfg))


def test_no_dns_warning_on_proxmox_sdn(make_config):
    cfg = make_config(
        {
            "name": "testc",
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {}}},
            },
        },
        remove=("openstack",),
    )
    # SDN gets DNS from network.dns, so there is nothing to warn about
    assert all("DHCP-backed" not in w for w in validate_warnings(cfg))


# ---------------------------------------------------------------------------
# cached_property semantics
# ---------------------------------------------------------------------------

def test_machines_is_cached_property(make_config):
    cfg = make_config()
    first = cfg.machines
    second = cfg.machines
    assert first is second


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------

def test_tags_default_empty(make_config):
    cfg = make_config()
    assert cfg.tags == {}
    assert cfg.machines["testcluster-controlplane-01"].tags == {}


def test_tags_cluster_wide_reach_every_machine(make_config):
    cfg = make_config({"tags": {"team": "platform"}})
    for m in cfg.machines.values():
        assert m.tags == {"team": "platform"}


def test_tags_pool_overrides_cluster(make_config):
    cfg = make_config({
        "tags": {"team": "platform", "tier": "shared"},
        "workers": {"worker": {
            "count": 1, "flavor": "f", "disk": 20,
            "tags": {"tier": "gpu"},
        }},
    })
    assert cfg.machines["testcluster-worker-01"].tags == {"team": "platform", "tier": "gpu"}
    assert cfg.machines["testcluster-controlplane-01"].tags == {
        "team": "platform", "tier": "shared"
    }


def test_tags_values_coerced_to_str(make_config):
    cfg = make_config({"tags": {"cost-center": 1234}})
    assert cfg.machines["testcluster-controlplane-01"].tags == {"cost-center": "1234"}


# ---------------------------------------------------------------------------
# provider selection and compatibility
# ---------------------------------------------------------------------------

def test_existing_openstack_yaml_loads_typed_provider(make_config):
    cfg = make_config()

    assert isinstance(cfg.provider, OpenStackConfig)
    assert cfg.provider_name == "openstack"
    assert cfg.openstack_url == "https://example.com:5000/v3/"
    assert cfg.availability_zone == "nova"
    assert cfg.external_net == "ext-net"
    # region defaults to RegionOne when cluster.yaml omits it
    assert cfg.region == "RegionOne"


def test_openstack_region_is_loaded_from_cluster_yaml(make_config):
    cfg = make_config({"openstack": {"region": "region-b"}})
    assert cfg.region == "region-b"


def test_a_provider_section_is_still_required(make_config):
    with pytest.raises(ConfigError, match="one provider section is required"):
        make_config(remove=("openstack",))


def test_at_most_one_vm_provider_is_allowed(make_config):
    with pytest.raises(ConfigError, match="at most one VM provider"):
        make_config({"proxmox": {"url": "https://pve.example"}})


def test_metal_section_loads_alongside_a_vm_provider(make_config):
    cfg = make_config({"metal": {
        "cp": {"role": "controlplane", "disk": "/dev/sda"},
        "worker": {"role": "worker", "disk": "/dev/sda"},
    }})

    assert isinstance(cfg.provider, OpenStackConfig)
    assert cfg.metal is not None
    assert cfg.metal.groups["cp"].role == "controlplane"
    assert cfg.metal.groups["worker"].role == "worker"
    # a group without its own `network` sits on the cluster L2
    assert cfg.metal.groups["worker"].network == cfg.network.cluster
    assert cfg.provider_name == "openstack"


def test_metal_without_a_vm_provider_is_refused(make_config):
    """A metal section always sits beside one VM provider: with none there is
    no backend to plan, converge, bootstrap or destroy the cluster, so the
    loader refuses the pair instead of failing mid-command."""
    with pytest.raises(ConfigError, match="metal section requires a VM provider"):
        make_config(
            {
                "controlplane": {"count": 3, "disk": 40},
                "workers": {"worker": {"count": 2, "disk": 100}},
                "metal": {
                    "worker": {
                        "role": "worker",
                        "disk": "/dev/sda",
                        "servers": {"rp001-worker": {}},
                    }
                },
            },
            remove=("openstack",),
        )


def test_metal_servers_flat_map_carries_every_server_role(make_config):
    """`metal_servers` is the desired-node view of the metal section: every
    server of every group, keyed by hostname with its role."""
    cfg = make_config({"metal": {
        "cp": {
            "role": "controlplane",
            "disk": "/dev/sda",
            "servers": {"rp001-cp": {}},
        },
        "worker": {
            "role": "worker",
            "disk": "/dev/sda",
            "servers": {"rp001-worker": {}, "rp002-worker": {}},
        },
    }})

    assert cfg.metal_servers == {
        "rp001-cp": "controlplane",
        "rp001-worker": "worker",
        "rp002-worker": "worker",
    }


def test_metal_servers_empty_without_a_metal_section(make_config):
    cfg = make_config()

    assert cfg.metal_servers == {}


@pytest.mark.parametrize(
    ("metal", "message"),
    [
        ([], "metal must be a YAML mapping"),
        ({"cp": "rp001"}, "metal.cp must be a YAML mapping"),
        ({1: {}}, "metal group names must be non-empty strings"),
    ],
)
def test_metal_section_shape_is_checked(make_config, metal, message):
    with pytest.raises(ConfigError, match=message):
        make_config({"metal": metal})


# ---------------------------------------------------------------------------
# the metal group schema
# ---------------------------------------------------------------------------

def test_metal_group_defaults(make_config):
    """`redfish` is off unless a group turns it on, and `network` is the cluster L2."""
    cfg = make_config({"metal": {"worker": {"role": "worker", "disk": "/dev/sda"}}})

    group = cfg.metal.groups["worker"]
    assert group.redfish is False
    assert group.network == cfg.network.cluster
    assert group.interfaces == {}
    assert group.bmc == MetalBmc()
    assert group.servers == {}


def test_metal_group_on_another_l2_requires_kubespan(make_config):
    """KubeSpan is the only path between the group's L2 and the cluster network."""
    with pytest.raises(ConfigError, match="talos.kubespan must be true"):
        make_config({
            "talos": {"kubespan": False},
            "metal": {"phoenix": {
                "role": "worker",
                "disk": "/dev/sda",
                "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1"},
            }},
        })


def test_metal_group_on_another_l2_requires_explicit_kubespan(make_config):
    """KubeSpan is off by default, so a group on another L2 must opt in even
    without naming the key: the unset default is refused like `false`."""
    with pytest.raises(ConfigError, match="talos.kubespan must be true"):
        make_config({
            "metal": {"phoenix": {
                "role": "worker",
                "disk": "/dev/sda",
                "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1"},
            }},
        })


def test_metal_group_on_the_cluster_l2_allows_kubespan_off(make_config):
    """A group on the cluster L2 -- by omission or by the same values -- needs no overlay."""
    cfg = make_config({
        "talos": {"kubespan": False},
        "metal": {
            "worker": {"role": "worker", "disk": "/dev/sda"},
            "same": {
                "role": "worker",
                "disk": "/dev/sda",
                "network": {"cidr": "192.168.0.0/21"},
            },
        },
    })

    assert cfg.metal.groups["worker"].network == cfg.network.cluster
    assert cfg.metal.groups["same"].network == cfg.network.cluster


def test_metal_group_on_the_cluster_subnet_needs_no_kubespan_despite_the_vip(make_config):
    """The L2 is the subnet: a group beside a cluster whose L2 carries a
    kubeapi_vip sits on the same L2, so the KubeSpan default (off) loads."""
    cfg = make_config({
        "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
        "proxmox": {
            "url": "https://pve.example:8006",
            "storage": "vms",
            "iso_storage": "isos",
            "network": {"cluster": {"bridge": "vmbr0"}},
        },
        "network": {"cluster": {
            "cidr": "172.29.21.0/24",
            "gateway": "172.29.21.1",
            "kubeapi_vip": "172.29.21.200",
        }},
        "metal": {"phoenix": {
            "role": "worker",
            "disk": "/dev/sda",
            "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1"},
        }},
    }, remove=("openstack",))

    assert cfg.kubespan is False


def test_metal_group_defaults_resolve_into_each_server(make_config):
    """Servers start from the group defaults; `bmc` and `interfaces` merge per key."""
    cfg = make_config({"talos": {"kubespan": True}, "metal": {"phoenix": {
        "role": "worker",
        "redfish": True,
        "disk": "/dev/sda",
        "network": {"cidr": "172.29.21.0/24", "gateway": "172.29.21.1", "mtu": 9000},
        "interfaces": {
            "enp1s0f0": {"role": "pxe"},
            "enp2s0f0": {"role": ["cluster", "external"], "dns": ["192.0.2.53"]},
        },
        "bmc": {"username": "root", "password": "secret"},
        "servers": {
            "rp001": {
                "bmc": {"ip": "172.28.50.5"},
                "interfaces": {"enp2s0f0": {"ip": "172.29.21.5/24"}},
            },
            "rp002": {"disk": "/dev/nvme0n1"},
        },
    }}})

    group = cfg.metal.groups["phoenix"]
    assert group.network == L2Network(
        cidr="172.29.21.0/24", gateway="172.29.21.1", mtu=9000
    )
    rp001 = group.servers["rp001"]
    assert rp001.group == "phoenix"
    assert rp001.role == "worker"
    assert rp001.redfish is True
    assert rp001.disk == "/dev/sda"
    assert rp001.network == group.network
    # the group's bmc credentials with the server's own BMC address merged in
    assert rp001.bmc == MetalBmc(ip="172.28.50.5", username="root", password="secret")
    # the group's interface roles with the server's own cluster address merged in
    assert rp001.interfaces == {
        "enp1s0f0": MetalInterface(role=("pxe",)),
        "enp2s0f0": MetalInterface(
            role=("cluster", "external"), ip="172.29.21.5/24", dns=("192.0.2.53",)
        ),
    }
    # rp002 overrides only its disk; everything else comes from the group
    rp002 = group.servers["rp002"]
    assert rp002.disk == "/dev/nvme0n1"
    assert rp002.bmc == group.bmc
    assert rp002.interfaces == group.interfaces


def test_metal_redfish_credentials_resolve_from_group_default_or_server_override(make_config):
    """A `redfish: true` machine takes its credentials from the group or its overrides."""
    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "redfish": True,
        "disk": "/dev/sda",
        "bmc": {"username": "root", "password": "secret"},
        "servers": {"rp001": {"bmc": {"ip": "172.28.50.5"}}},
    }}})
    rp001 = cfg.metal.groups["phoenix"].servers["rp001"]
    assert rp001.bmc == MetalBmc(ip="172.28.50.5", username="root", password="secret")

    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "redfish": True,
        "disk": "/dev/sda",
        "servers": {
            "rp001": {"bmc": {"ip": "172.28.50.5", "username": "admin", "password": "s3cret"}},
        },
    }}})
    rp001 = cfg.metal.groups["phoenix"].servers["rp001"]
    assert rp001.bmc == MetalBmc(ip="172.28.50.5", username="admin", password="s3cret")


@pytest.mark.parametrize("source", ["secrets.yaml", "an include", "cluster.yaml"])
def test_metal_redfish_credentials_load_from_whichever_file_supplies_them(
    make_config, tmp_path, source
):
    """Where a BMC credential is written is the user's choice, not the schema's."""
    credentials = {"username": "root", "password": "secret"}
    overrides: dict = {"metal": {"phoenix": {
        "role": "worker",
        "redfish": True,
        "disk": "/dev/sda",
        "servers": {"rp001": {"bmc": {"ip": "172.28.50.5"}}},
    }}}
    if source == "secrets.yaml":
        _write_secrets(tmp_path, {"metal": {"phoenix": {"bmc": credentials}}})
    elif source == "an include":
        (tmp_path / "bmc.yaml").write_text(
            yaml.safe_dump({"metal": {"phoenix": {"bmc": credentials}}})
        )
        overrides["include"] = ["bmc.yaml"]
    else:
        overrides["metal"]["phoenix"]["bmc"] = credentials

    cfg = make_config(overrides)
    rp001 = cfg.metal.groups["phoenix"].servers["rp001"]
    assert (rp001.bmc.username, rp001.bmc.password) == ("root", "secret")


@pytest.mark.parametrize(
    ("bmc", "message"),
    [
        (
            {"ip": "172.28.50.5"},
            r"metal\.phoenix\.servers\.rp001: bmc\.username must be a non-empty string",
        ),
        (
            {"ip": "172.28.50.5", "username": "root"},
            r"metal\.phoenix\.servers\.rp001: bmc\.password must be a non-empty string",
        ),
        (
            {"ip": "172.28.50.5", "username": "root", "password": ""},
            r"metal\.phoenix\.servers\.rp001: bmc\.password must be a non-empty string",
        ),
        (
            {"ip": "172.28.50.5", "username": "CHANGE-ME", "password": "secret"},
            r"metal\.phoenix\.servers\.rp001: bmc\.username is still the scaffolded "
            r"'CHANGE-ME' placeholder",
        ),
        (
            {"ip": "172.28.50.5", "username": "root", "password": "CHANGE-ME"},
            r"metal\.phoenix\.servers\.rp001: bmc\.password is still the scaffolded "
            r"'CHANGE-ME' placeholder",
        ),
    ],
)
def test_metal_redfish_rejects_missing_or_placeholder_credentials(make_config, bmc, message):
    """A `redfish: true` machine must end up with real BMC credentials."""
    with pytest.raises(ConfigError, match=message):
        make_config({"metal": {"phoenix": {
            "role": "worker",
            "redfish": True,
            "disk": "/dev/sda",
            "servers": {"rp001": {"bmc": bmc}},
        }}})


def test_metal_without_redfish_needs_no_bmc_credentials(make_config):
    """`redfish: false` never touches the BMC, so credentials may stay unset."""
    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "disk": "/dev/sda",
        "servers": {"rp001": {"bmc": {"ip": "172.28.50.5", "username": "CHANGE-ME"}}},
    }}})

    rp001 = cfg.metal.groups["phoenix"].servers["rp001"]
    assert rp001.bmc == MetalBmc(ip="172.28.50.5", username="CHANGE-ME")


def test_metal_server_opting_out_of_redfish_skips_the_credentials(make_config):
    """The requirement follows the merged flag, so a server may turn redfish off."""
    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "redfish": True,
        "disk": "/dev/sda",
        "servers": {"rp001": {"redfish": False, "bmc": {"ip": "172.28.50.5"}}},
    }}})

    assert cfg.metal.groups["phoenix"].servers["rp001"].redfish is False


def test_metal_server_can_add_an_interface(make_config):
    """A server's interfaces merge with the group's per name, adding new ones."""
    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": {"enp1s0f0": {"role": "pxe", "ip": "172.29.21.9/24"}},
        "servers": {"rp001": {"interfaces": {"enp2s0f0": {"role": "cluster"}}}},
    }}})

    assert cfg.metal.groups["phoenix"].servers["rp001"].interfaces == {
        "enp1s0f0": MetalInterface(role=("pxe",), ip="172.29.21.9/24"),
        "enp2s0f0": MetalInterface(role=("cluster",)),
    }


def test_metal_interface_can_override_the_vlan_child(make_config):
    """`link_name` and `vlan` name and tag the external link's VLAN child."""
    cfg = make_config({"metal": {"phoenix": {
        "role": "worker",
        "disk": "/dev/sda",
        "interfaces": {
            "enp2s0f0": {"role": ["cluster", "external"], "link_name": "ext0", "vlan": 1600},
        },
        "servers": {"rp001": {"interfaces": {"enp2s0f0": {"vlan": 1691}}}},
    }}})

    group = cfg.metal.groups["phoenix"]
    assert group.interfaces["enp2s0f0"] == MetalInterface(
        role=("cluster", "external"), link_name="ext0", vlan=1600,
    )
    # the server's own override replaces the group's vlan, keeps its link name
    assert group.servers["rp001"].interfaces["enp2s0f0"] == MetalInterface(
        role=("cluster", "external"), link_name="ext0", vlan=1691,
    )


@pytest.mark.parametrize(
    ("metal", "message"),
    [
        ({"worker": {"disk": "/dev/sda"}}, r"metal\.worker: missing 'role'"),
        (
            {"worker": {"role": "master", "disk": "/dev/sda"}},
            r"metal\.worker\.role must be 'controlplane' or 'worker'",
        ),
        ({"worker": {"role": "worker"}}, r"metal\.worker: missing 'disk'"),
        (
            {"worker": {"role": "worker", "disk": " "}},
            r"metal\.worker\.disk must be a non-empty string",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "redfish": "yes"}},
            r"metal\.worker\.redfish must be true or false",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "driver": "redfish"}},
            r"metal\.worker: unknown key\(s\): driver",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "network": {"cidr": "nope"}}},
            r"metal\.worker\.network\.cidr is not a valid network: 'nope'",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "network": {"cidr": "172.29.21.0/24", "anchor_cidr": "169.254.40.0/24"},
                }
            },
            r"metal\.worker\.network: unknown key\(s\): anchor_cidr",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"speed": 10000}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0: unknown key\(s\): speed",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": "mgmt"}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.role must be 'cluster', 'external', "
            r"'pxe', or a list of those",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": ["cluster", "mgmt"]}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.role must be 'cluster', 'external', "
            r"'pxe', or a list of those",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": []}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.role must be 'cluster', 'external', "
            r"'pxe', or a list of those",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": "cluster", "ip": "nope"}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.ip is invalid: 'nope'",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {
                        "enp1s0f0": {"role": "cluster", "dns": ["dns.example.edu"]}
                    },
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.dns contains an invalid address",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": "cluster", "link_name": " "}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.link_name must be a non-empty string",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": "cluster", "vlan": 4095}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.vlan must be 1-4094",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "interfaces": {"enp1s0f0": {"role": "cluster", "vlan": True}},
                }
            },
            r"metal\.worker\.interfaces\.enp1s0f0\.vlan must be 1-4094",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "bmc": {"user": "root"}}},
            r"metal\.worker\.bmc: unknown key\(s\): user",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "bmc": {"ip": "bmc.example.edu"}}},
            r"metal\.worker\.bmc\.ip is invalid: 'bmc\.example\.edu'",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "servers": ["rp001"]}},
            r"metal\.worker\.servers must be a YAML mapping",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "servers": {"rp 001": {}}}},
            r"server name 'rp 001' is not a valid hostname component",
        ),
        (
            {"worker": {"role": "worker", "disk": "/dev/sda", "servers": {1: {}}}},
            r"metal\.worker\.servers: server names must be non-empty strings",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "servers": {"rp001": {"user": "x"}},
                }
            },
            r"metal\.worker\.servers\.rp001: unknown key\(s\): user",
        ),
        (
            {
                "worker": {
                    "role": "worker",
                    "disk": "/dev/sda",
                    "servers": {"rp001": {"role": "controlplane", "role2": "x"}},
                }
            },
            r"metal\.worker\.servers\.rp001: unknown key\(s\): role2",
        ),
    ],
)
def test_metal_schema_is_checked(make_config, metal, message):
    with pytest.raises(ConfigError, match=message):
        make_config({"metal": metal})


def test_metal_server_name_must_be_unique_across_groups(make_config):
    group = {"role": "worker", "disk": "/dev/sda", "servers": {"rp001": {}}}
    with pytest.raises(
        ConfigError, match=r"metal server 'rp001' is defined in more than one group"
    ):
        make_config({
            "metal": {
                "a": group,
                "b": {**group, "servers": {"rp001": {}, "rp002": {}}},
            }
        })


# ---------------------------------------------------------------------------
# one VM provider plus metal
# ---------------------------------------------------------------------------

def _metal_groups() -> dict:
    """One bare-metal group per role, in the shape the metal provider will own."""
    return {
        "cp": {
            "role": "controlplane",
            "redfish": True,
            "disk": "/dev/sda",
            "interfaces": {
                "enp1s0f0": {"role": "pxe"},
                "enp2s0f0": {"role": "cluster"},
            },
            "bmc": {"username": "root", "password": "secret"},
            "servers": {"rp001-cp": {"bmc": {"ip": "192.0.2.10"}}},
        },
        "worker": {
            "role": "worker",
            "redfish": True,
            "disk": "/dev/sda",
            "interfaces": {
                "enp1s0f0": {"role": "pxe"},
                "enp2s0f0": {"role": "cluster"},
            },
            "bmc": {"username": "root", "password": "secret"},
            "servers": {"rp001-worker": {"bmc": {"ip": "192.0.2.11"}}},
        },
    }


def _expected_metal_groups(cluster: L2Network) -> MetalConfig:
    """`_metal_groups()` as the loader resolves it: typed, merges applied.

    The group `network` defaults to the cluster L2, so the caller passes the
    one its cluster carries.
    """
    def group(name: str, role: str, server: MetalServer) -> MetalGroup:
        return MetalGroup(
            name=name,
            role=role,
            redfish=True,
            disk="/dev/sda",
            network=cluster,
            interfaces={
                "enp1s0f0": MetalInterface(role=("pxe",)),
                "enp2s0f0": MetalInterface(role=("cluster",)),
            },
            bmc=MetalBmc(username="root", password="secret"),
            servers={server.name: server},
        )

    def server(name: str, group: str, role: str, bmc_ip: str) -> MetalServer:
        return MetalServer(
            name=name,
            group=group,
            role=role,
            disk="/dev/sda",
            network=cluster,
            redfish=True,
            interfaces={
                "enp1s0f0": MetalInterface(role=("pxe",)),
                "enp2s0f0": MetalInterface(role=("cluster",)),
            },
            bmc=MetalBmc(ip=bmc_ip, username="root", password="secret"),
        )

    return MetalConfig(groups={
        "cp": group(
            "cp", "controlplane",
            server("rp001-cp", "cp", "controlplane", "192.0.2.10"),
        ),
        "worker": group(
            "worker", "worker",
            server("rp001-worker", "worker", "worker", "192.0.2.11"),
        ),
    })


def test_openstack_with_metal_loads(make_config):
    """A mixed cluster: OpenStack VMs plus bare-metal groups beside them."""
    cfg = make_config({
        "workers": {"worker": {"count": 2, "flavor": "gp.xlarge", "disk": 100}},
        "metal": _metal_groups(),
    })

    assert isinstance(cfg.provider, OpenStackConfig)
    assert cfg.provider_name == "openstack"
    assert cfg.metal == _expected_metal_groups(cfg.network.cluster)
    assert cfg.network.external is None
    # the pools expand regardless of which side each machine lands on
    assert len(cfg.machines) == 5


def test_proxmox_with_metal_loads(make_config):
    """A mixed cluster: Proxmox VMs on a flat bridge plus bare-metal groups."""
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
            "workers": {"worker": {"count": 2, "cores": 8, "memory": 16, "disk": 100}},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "nodes": ["pve001", "pve002"],
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
            "metal": _metal_groups(),
        },
        remove=("openstack",),
    )

    assert isinstance(cfg.provider, ProxmoxConfig)
    assert cfg.provider_name == "proxmox"
    assert cfg.metal == _expected_metal_groups(cfg.network.cluster)
    assert cfg.network.cluster.kubeapi_vip == "192.168.0.10"
    assert len(cfg.machines) == 5


def test_proxmox_sdn_with_metal_loads(make_config):
    """A mixed cluster: SDN-managed VMs plus bare-metal groups on the same L2."""
    overrides = _proxmox_sdn_overrides()
    overrides["metal"] = _metal_groups()
    cfg = make_config(overrides, remove=("openstack",))

    assert isinstance(cfg.provider, ProxmoxConfig)
    assert proxmox_sdn(cfg.name, cfg.provider) is not None
    assert cfg.metal == _expected_metal_groups(cfg.network.cluster)
    assert len(cfg.machines) == 1


def test_openstack_with_metal_rejects_network_external(make_config):
    """OpenStack allocates the external network itself, with or without metal."""
    with pytest.raises(ConfigError, match="network.external is not valid with openstack"):
        make_config({
            "metal": _metal_groups(),
            "network": {"external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "anchor_cidr": "169.254.40.0/24",
            }},
        })


def test_proxmox_provider_section_is_typed(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
            "workers": {
                "worker": {"count": 1, "cores": 8, "memory": 16, "disk": 100}
            },
            "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "placement_strategy": "spread",
                "nodes": ["pve001", "pve002"],
                "network": {
                    "cluster": {"bridge": "vmbr0"},
                },
            },
        },
        remove=("openstack",),
    )

    assert isinstance(cfg.provider, ProxmoxConfig)
    assert cfg.provider_name == "proxmox"
    assert cfg.provider.storage == "vms"
    assert cfg.provider.cidata_storage == "local"
    assert cfg.provider.nodes == ("pve001", "pve002")
    assert cfg.provider.network["cluster"]["bridge"] == "vmbr0"
    assert cfg.machines["testcluster-controlplane-01"].cores == 4
    assert cfg.machines["testcluster-worker-01"].memory == 16
    assert cfg.machines["testcluster-worker-01"].flavor == ""


@pytest.mark.parametrize(
    ("proxmox", "network", "message"),
    [
        (
            {
                "url": "https://pve.example",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "proxmox.storage",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "vnet": "talos"}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "exactly one of bridge, vnet, or sdn",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
            {"cluster": {"kubeapi_vip": "203.0.113.10"}},
            "inside network.cluster.cidr",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "sdn": {}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "mutually exclusive",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {"zone": "vlan"}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "only supports 'evpn'",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {"vrf_tag": 16777215}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "set an explicit tag",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {"nodes": ["pve001"], "exit_nodes": ["pve002"]}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "exit_nodes must be members",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "nodes": ["pve001", "pve003"],
                "network": {"cluster": {"sdn": {"nodes": ["pve001", "pve002"]}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "must include every proxmox.nodes",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.1"}},
            "collides with the SDN anycast gateway",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.11"}},
            "collides with the static address",
        ),
        (
            # .12 is cp-02's slot: unallocated today, reserved by the layout
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.12"}},
            "sits inside the SDN static address layout",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"sdn": {"vrf_tag": 100, "tag": 100}}},
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "tag must differ from vrf_tag",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {
                    "cluster": {
                        "sdn": {
                            "exit_nodes": ["pve001", "pve002"],
                            "primary_exit_node": "pve003",
                        },
                    },
                },
            },
            {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "primary_exit_node must be one of the exit nodes",
        ),
    ],
)
def test_proxmox_compute_configuration_is_validated(make_config, proxmox, network, message):
    with pytest.raises(ConfigError, match=message):
        make_config(
            {
                "name": "testc",  # sdn cases need a name that fits the SDN id format
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": network,
                "proxmox": proxmox,
            },
            remove=("openstack",),
        )


def test_proxmox_sdn_rejects_cluster_name_unfit_for_sdn_ids(make_config):
    # the zone/VNet are named after the cluster: 2-8 chars, no hyphens
    with pytest.raises(ConfigError, match="cannot be used as the SDN zone/VNet"):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": {"cluster": {"kubeapi_vip": "192.168.0.9"}},
                "proxmox": {
                    "url": "https://pve.example",
                    "storage": "vms",
                    "iso_storage": "isos",
                    "network": {"cluster": {"sdn": {}}},
                },
            },
            remove=("openstack",),
        )


def _proxmox_external_overrides() -> dict:
    """A valid external network, in the shape cluster.yaml uses today."""
    return {
        "network": {
            "external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "anchor_cidr": "169.254.40.0/24",
                "ingress_pool": "203.0.113.20-203.0.113.40",
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
    }


def test_proxmox_external_section_validates(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            **_proxmox_external_overrides(),
        },
        remove=("openstack",),
    )
    assert cfg.network.external.kubeapi_vip == "203.0.113.10"


def test_proxmox_external_makes_cluster_kubeapi_vip_optional(make_config):
    # cluster section has only bridge, no kubeapi_vip — valid when external is present
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            **_proxmox_external_overrides(),
        },
        remove=("openstack",),
    )
    assert cfg.network.cluster.kubeapi_vip == ""


def test_proxmox_vip_can_be_in_cluster_with_external_present(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {
                "cluster": {"kubeapi_vip": "192.168.0.10"},
                "external": {
                    "cidr": "203.0.113.0/24",
                    "gateway": "203.0.113.1",
                    "anchor_cidr": "169.254.40.0/24",
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
    assert cfg.network.cluster.kubeapi_vip == "192.168.0.10"
    assert cfg.network.external.kubeapi_vip == ""


def test_proxmox_vip_rejected_when_in_both_sections(make_config):
    with pytest.raises(ConfigError, match="only one of network.cluster or network.external"):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": {
                    "cluster": {"kubeapi_vip": "192.168.0.10"},
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


def test_proxmox_vip_rejected_when_in_neither_section(make_config):
    with pytest.raises(ConfigError, match="kubeapi_vip must be set"):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": {
                    "external": {
                        "cidr": "203.0.113.0/24",
                        "gateway": "203.0.113.1",
                        "anchor_cidr": "169.254.40.0/24",
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


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bridge": ""}, "external.bridge"),
        ({"cidr": ""}, "network.external.cidr"),
        ({"gateway": "10.0.0.1"}, "gateway must be inside network.external.cidr"),
        ({"anchor_cidr": "10.0.0.0/24"}, "anchor_cidr.*169.254.0.0/16"),
        ({"kubeapi_vip": "192.168.0.10"}, "kubeapi_vip must be inside network.external.cidr"),
        ({"ingress_pool": "203.0.113.50-203.0.113.10"}, "start must be <= end"),
        ({"ingress_pool": "203.0.113.50-999.999.999.999"}, "ingress_pool end is invalid"),
        (
            {"kubeapi_vip": "203.0.113.30", "ingress_pool": "203.0.113.20-203.0.113.40"},
            "kubeapi_vip must not be inside ingress_pool",
        ),
    ],
)
def test_proxmox_external_section_rejects_invalid_fields(make_config, overrides, message):
    base = _proxmox_external_overrides()
    for key, value in overrides.items():
        # the bridge is Proxmox plumbing; every other key describes the L2
        section = base["proxmox"]["network"] if key == "bridge" else base["network"]
        section["external"][key] = value
    with pytest.raises(ConfigError, match=message):
        make_config(
            {"controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40}, **base},
            remove=("openstack",),
        )


def _write_secrets(root: Path, secrets: dict) -> None:
    """Write the gitignored secrets.yaml the loader includes implicitly."""
    (root / SECRETS_FILE).write_text(yaml.safe_dump(secrets))


OPENSTACK_CREDENTIALS = {"credential_id": "id", "credential_secret": "secret"}
PROXMOX_CREDENTIALS = {"token_id": "user@pve!provider", "token_secret": "secret"}


def test_openstack_credentials_are_read_from_secrets_yaml(make_config, tmp_path):
    _write_secrets(tmp_path, {"openstack": dict(OPENSTACK_CREDENTIALS)})

    cfg = make_config()

    assert cfg.openstack_credentials == ("id", "secret")
    assert cfg.provider.credential_id == "id"


def test_proxmox_credentials_are_read_from_secrets_yaml(make_config, tmp_path):
    _write_secrets(tmp_path, {"proxmox": dict(PROXMOX_CREDENTIALS)})

    cfg = make_config(
        {"controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
         "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
         "proxmox": {"url": "https://pve.example", "storage": "vms",
                     "iso_storage": "isos",
                     "network": {"cluster": {"bridge": "vmbr0"}}}},
        remove=("openstack",),
    )

    assert cfg.provider.credentials() == ("user@pve!provider", "secret")


@pytest.mark.parametrize("source", ["secrets.yaml", "an include", "cluster.yaml"])
def test_credentials_load_from_whichever_file_supplies_them(make_config, tmp_path, source):
    """Where a credential is written is the user's choice, not the schema's."""
    overrides: dict = {}
    if source == "secrets.yaml":
        _write_secrets(tmp_path, {"openstack": dict(OPENSTACK_CREDENTIALS)})
    elif source == "an include":
        (tmp_path / "creds.yaml").write_text(
            yaml.safe_dump({"openstack": dict(OPENSTACK_CREDENTIALS)})
        )
        overrides["include"] = ["creds.yaml"]
    else:
        overrides["openstack"] = dict(OPENSTACK_CREDENTIALS)

    assert make_config(overrides).openstack_credentials == ("id", "secret")


def test_a_credential_set_in_two_files_names_both(make_config, tmp_path):
    _write_secrets(tmp_path, {"openstack": {"credential_id": "from-secrets"}})

    with pytest.raises(
        ConfigError,
        match="openstack.credential_id is set in both cluster.yaml and secrets.yaml",
    ):
        make_config({"openstack": {"credential_id": "from-cluster"}})


def test_credentials_are_only_required_when_a_command_needs_them(make_config):
    """`check` and friends load a cluster without any credential configured."""
    cfg = make_config()

    assert cfg.provider.credential_id == ""
    with pytest.raises(ConfigError, match="credential_id must be a non-empty string"):
        assert cfg.openstack_credentials


@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({"credential_id": None, "credential_secret": "secret"},
         "credential_id must be a non-empty string"),
        ({"credential_id": 123, "credential_secret": "secret"},
         "credential_id must be a non-empty string"),
        ({"credential_id": "id", "credential_secret": None},
         "credential_secret must be a non-empty string"),
        ({"credential_id": "id", "credential_secret": ""},
         "credential_secret must be a non-empty string"),
        ({"credential_id": "CHANGE-ME", "credential_secret": "secret"}, "CHANGE-ME"),
        ({"credential_id": "id", "credential_secret": "CHANGE-ME"}, "CHANGE-ME"),
    ],
)
def test_openstack_credentials_reject_invalid_values(make_config, tmp_path,
                                                     credentials, message):
    _write_secrets(tmp_path, {"openstack": credentials})
    cfg = make_config()
    with pytest.raises(ConfigError, match=message):
        assert cfg.openstack_credentials


@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({"token_id": None, "token_secret": "secret"},
         "token_id must be a non-empty string"),
        ({"token_id": "u@pve!t", "token_secret": 42},
         "token_secret must be a non-empty string"),
        ({"token_id": "u@pve!t", "token_secret": "CHANGE-ME"}, "CHANGE-ME"),
    ],
)
def test_proxmox_credentials_reject_invalid_values(make_config, tmp_path,
                                                   credentials, message):
    _write_secrets(tmp_path, {"proxmox": credentials})
    cfg = make_config(
        {"controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
         "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
         "proxmox": {"url": "https://pve.example", "storage": "vms",
                     "iso_storage": "isos",
                     "network": {"cluster": {"bridge": "vmbr0"}}}},
        remove=("openstack",),
    )
    with pytest.raises(ConfigError, match=message):
        cfg.provider.credentials()


def test_the_other_providers_credentials_are_still_refused(make_config, tmp_path):
    """A secrets.yaml for the wrong provider now trips the one-provider rule."""
    _write_secrets(tmp_path, {"proxmox": dict(PROXMOX_CREDENTIALS)})

    with pytest.raises(ConfigError, match="at most one VM provider"):
        make_config()


def test_tailscale_auth_key_may_be_omitted(make_config, tmp_path):
    """An absent tailscale.auth_key leaves the tailscale extension idle."""
    _write_secrets(tmp_path, {"openstack": dict(OPENSTACK_CREDENTIALS)})

    assert make_config().tailscale_auth_key is None
    assert make_config({"tailscale": {"auth_key": None}}).tailscale_auth_key is None


def test_tailscale_auth_key_loads_and_rejects_placeholders(make_config, tmp_path):
    _write_secrets(
        tmp_path,
        {"openstack": dict(OPENSTACK_CREDENTIALS),
         "tailscale": {"auth_key": "tskey-auth-abc123"}},
    )
    assert make_config().tailscale_auth_key == "tskey-auth-abc123"

    for bad, message in (
        (42, "tailscale.auth_key must be a non-empty string"),
        ("CHANGE-ME", "CHANGE-ME"),
        ("", "tailscale.auth_key must be a non-empty string"),
    ):
        _write_secrets(
            tmp_path,
            {"openstack": dict(OPENSTACK_CREDENTIALS), "tailscale": {"auth_key": bad}},
        )
        cfg = make_config()
        with pytest.raises(ConfigError, match=message):
            assert cfg.tailscale_auth_key


# ---------------------------------------------------------------------------
# security rules
# ---------------------------------------------------------------------------

def test_security_legacy_name_to_cidr_shape_still_loads(make_config):
    cfg = make_config({"security": {
        "kubernetes": {"vpn": "172.16.0.0/16"},
        "talos": {"vpn": "172.16.0.0/16"},
    }})
    assert cfg.security["kubernetes"].port == 6443
    assert cfg.security["talos"].port == 50000
    assert cfg.security["kubernetes"].hosts == {"vpn": "172.16.0.0/16"}
    # the pre-Stage-4 accessors keep working
    assert cfg.security_kubernetes == {"vpn": "172.16.0.0/16"}
    assert cfg.security_talos == {"vpn": "172.16.0.0/16"}


def test_security_named_rule_requires_explicit_port(make_config):
    with pytest.raises(ConfigError, match="requires an explicit 'port'"):
        make_config({"security": {"metrics": {"hosts": {"vpn": "172.16.0.0/16"}}}})


def test_security_named_rule_with_port(make_config):
    cfg = make_config({"security": {
        "metrics": {"port": 9100, "hosts": {"vpn": "172.16.0.0/16"}},
    }})
    assert cfg.security["metrics"] == SecurityRule(
        name="metrics", port=9100, hosts={"vpn": "172.16.0.0/16"}
    )


def test_security_hosts_shape_accepts_default_ports(make_config):
    cfg = make_config({"security": {
        "talos": {"hosts": {"vpn": "172.16.0.0/16"}},
    }})
    assert cfg.security["talos"].port == 50000


def test_security_rejects_unknown_keys(make_config):
    with pytest.raises(ConfigError, match="unknown keys: protocol"):
        make_config({"security": {
            "talos": {"hosts": {"vpn": "172.16.0.0/16"}, "protocol": "udp"},
        }})


def test_security_rejects_out_of_range_port(make_config):
    with pytest.raises(ConfigError, match="port must be 1-65535"):
        make_config({"security": {"metrics": {"port": 70000, "hosts": {}}}})


def test_http_and_https_are_open_by_default(make_config):
    cfg = make_config({"security": {"talos": {"vpn": "172.16.0.0/16"}}})
    assert cfg.open_ports() == (80, 443)


def test_http_block_closes_the_default_open_port(make_config):
    cfg = make_config({"security": {
        "http": {"hosts": {"office": "203.0.113.0/24"}},
    }})
    assert cfg.open_ports() == (443,)
    assert cfg.security["http"].port == 80


def test_https_block_closes_the_default_open_port(make_config):
    cfg = make_config({"security": {"https": {"hosts": {}}}})
    assert cfg.open_ports() == (80,)
    assert cfg.security["https"].port == 443
    assert cfg.security["https"].hosts == {}


def test_security_rejects_invalid_cidr_on_a_named_rule(make_config):
    with pytest.raises(ConfigError, match="security.metrics.vpn has invalid CIDR"):
        make_config({"security": {"metrics": {"port": 9100, "hosts": {"vpn": "nope"}}}})


def test_http_and_https_cannot_change_their_port(make_config):
    """`http`/`https` name the port they govern; another port needs another name."""
    with pytest.raises(ConfigError, match="cannot change its port from 80 to 8080"):
        make_config({"security": {
            "http": {"port": 8080, "hosts": {"office": "203.0.113.0/24"}},
        }})
    with pytest.raises(ConfigError, match="cannot change its port from 443 to 8443"):
        make_config({"security": {"https": {"port": 8443, "hosts": {}}}})


def test_http_and_https_may_restate_their_default_port(make_config):
    cfg = make_config({"security": {
        "https": {"port": 443, "hosts": {"office": "203.0.113.0/24"}},
    }})
    assert cfg.open_ports() == (80,)


def test_any_rule_claiming_443_closes_the_default_open_port(make_config):
    cfg = make_config({"security": {
        "ingress": {"port": 443, "hosts": {"office": "203.0.113.0/24"}},
    }})
    assert cfg.open_ports() == (80,)


def _proxmox_sdn_overrides(sdn: dict | None = None) -> dict:
    return {
        "name": "testc",
        "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
        "network": {"cluster": {"kubeapi_vip": "192.168.0.9"}},
        "proxmox": {
            "url": "https://pve.example",
            "storage": "vms",
            "iso_storage": "isos",
            "nodes": ["pve001", "pve002"],
            "network": {
                "cluster": {"sdn": sdn if sdn is not None else {}}
            },
        },
    }


def test_proxmox_sdn_empty_mapping_resolves_all_defaults(make_config):
    cfg = make_config(_proxmox_sdn_overrides(), remove=("openstack",))
    sdn = proxmox_sdn(cfg.name, cfg.provider)
    assert sdn is not None
    assert sdn.zone == "evpn"
    assert sdn.controller == "evpnctl"
    assert sdn.asn == 65000
    assert sdn.vrf_tag == naming.sdn_vni("testc")
    assert sdn.tag == sdn.vrf_tag + 1
    assert sdn.exit_nodes == ("pve001", "pve002")
    assert sdn.primary_exit_node == "pve001"
    assert sdn.mtu is None and sdn.nodes == ()


def test_proxmox_sdn_absent_resolves_to_none(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            "network": {"cluster": {"kubeapi_vip": "192.168.0.9"}},
            "proxmox": {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0"}},
            },
        },
        remove=("openstack",),
    )
    assert proxmox_sdn(cfg.name, cfg.provider) is None


def test_proxmox_sdn_requires_dns(make_config):
    overrides = _proxmox_sdn_overrides()
    overrides["network"] = {"dns": []}
    with pytest.raises(ConfigError, match="network.dns is required"):
        make_config(overrides, remove=("openstack",))


def test_proxmox_sdn_name_overrides_the_cluster_name(make_config):
    overrides = _proxmox_sdn_overrides({"name": "grid"})
    overrides["name"] = "a-cluster-name-too-long-for-sdn-ids"
    cfg = make_config(overrides, remove=("openstack",))
    sdn = proxmox_sdn(cfg.name, cfg.provider)
    assert sdn is not None and sdn.name == "grid"


def test_proxmox_sdn_vip_inside_a_worker_pool_block_is_rejected(make_config):
    # .65 is worker-05's slot: unallocated today (one worker), but the pool's
    # whole 50-address block is reserved, so scaling to it would collide
    overrides = _proxmox_sdn_overrides()
    overrides["workers"] = {"worker": {"count": 1, "cores": 4, "memory": 8, "disk": 40}}
    overrides["network"]["cluster"]["kubeapi_vip"] = "192.168.0.65"
    with pytest.raises(ConfigError, match="sits inside the SDN static address layout"):
        make_config(overrides, remove=("openstack",))


# ---------------------------------------------------------------------------
# top-level key validation (misspelled / unsupported keys, plugin sections)
# ---------------------------------------------------------------------------

def test_unknown_top_level_cluster_key_is_rejected(make_config):
    with pytest.raises(ConfigError, match=r"unknown key\(s\): clustrer"):
        make_config({"clustrer": "oops"})


def test_unknown_top_level_cluster_key_lists_every_unknown(make_config):
    with pytest.raises(ConfigError, match=r"unknown key\(s\): anemia, mistkes"):
        make_config({"anemia": 1, "mistkes": 2})


def test_installed_plugin_sections_are_retained(make_config):
    """An `argocd:` / `rancher:` section is valid because the plugin owns it."""
    cfg = make_config({"argocd": {"admins": [], "users": []},
                       "rancher": {"admins": [], "users": []}})
    assert cfg.raw["argocd"]["admins"] == []


def test_unknown_top_level_secrets_key_is_rejected(make_config, tmp_path):
    """secrets.yaml follows the cluster.yaml schema, and names itself on a typo."""
    _write_secrets(tmp_path, {"taliscla": {"auth_key": "x"}})
    with pytest.raises(ConfigError, match=r"secrets.yaml: unknown key\(s\): taliscla"):
        make_config()


def test_unknown_top_level_secrets_plugin_key_still_rejected(make_config, tmp_path):
    """A section that no installed plugin owns is unknown, not a valid retention."""
    _write_secrets(tmp_path, {"gitlab": {"url": "x"}})
    with pytest.raises(ConfigError, match=r"secrets.yaml: unknown key\(s\): gitlab"):
        make_config()


# ---------------------------------------------------------------------------
# nested-key validation inside fixed-schema sections (typo / unsupported key)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"talos": {"extensons": ["siderolabs/foo"]}}, r"talos: unknown key\(s\): extensons"),
        ({"network": {"dnss": ["9.9.9.9"]}}, r"network: unknown key\(s\): dnss"),
        ({"openstack": {"regoin": "RegionTwo"}}, r"openstack: unknown key\(s\): regoin"),
        ({"kubernetes": {"verson": "v1.31.0"}}, r"kubernetes: unknown key\(s\): verson"),
        ({"tailscale": {"login_serer": "https://hs.example"}},
         r"tailscale: unknown key\(s\): login_serer"),
        ({"controlplane": {"count": 3, "flavor": "gp.medium", "disk": 40, "flavr": "x"}},
         r"controlplane: unknown key\(s\): flavr"),
        ({"workers": {"worker": {"count": 1, "flavor": "f", "disk": 20, "cors": 4}}},
         r"workers.worker: unknown key\(s\): cors"),
    ],
)
def test_unknown_nested_key_is_rejected(make_config, overrides, field):
    """A miscapped or unsupported key inside a fixed-schema section no longer
    loads and is silently ignored."""
    with pytest.raises(ConfigError, match=field):
        make_config(overrides)


def test_typo_region_no_longer_silently_selects_regionone(make_config):
    """A misspelled `openstack.regoin` is refused instead of silently selecting
    the `RegionOne` default, which the region typo previously did."""
    with pytest.raises(ConfigError, match=r"openstack: unknown key\(s\): regoin"):
        make_config({"openstack": {"regoin": "RegionTwo"}})


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"network": {"cluster": {"bridge": "vmbr0"}, "clustr": {}}},
         r"proxmox.network: unknown key\(s\): clustr"),
        ({"network": {"cluster": {"bridge": "vmbr0", "vlna": 321}}},
         r"proxmox.network.cluster: unknown key\(s\): vlna"),
        ({"network": {"cluster": {"bridge": "vmbr0"}, "external": {"bridge": "vmbr1"}}},
         None),  # valid plumbing, no error
        ({"network": {"cluster": {"bridge": "vmbr0"},
                      "external": {"bridge": "vmbr1", "bridg": "vmbr2"}}},
         r"proxmox.network.external: unknown key\(s\): bridg"),
        ({"network": {"cluster": {"sdn": {"muta": 8950}}}},
         r"proxmox.network.cluster.sdn: unknown key\(s\): muta"),
        ({"network": {"cluster": {"sdn": {"exit_ndoes": ["pve1"]}}}},
         r"proxmox.network.cluster.sdn: unknown key\(s\): exit_ndoes"),
    ],
)
def test_proxmox_network_nested_key_is_rejected(make_config, overrides, field):
    """A miscapped or unsupported key inside a `proxmox.network` block is refused
    instead of loading and being silently ignored."""
    external = "external" in overrides["network"]
    cfg_overrides = {
        "name": "testc",  # the sdn cases need a name that fits the SDN id format
        "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
        "network": {
            "cluster": {"kubeapi_vip": "192.168.0.9"},
            **({"external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "anchor_cidr": "169.254.40.0/24",
                "ingress_pool": "203.0.113.20-203.0.113.40",
            }} if external else {}),
        },
        "proxmox": {
            "url": "https://pve.example",
            "storage": "vms",
            "iso_storage": "isos",
            "network": overrides["network"],
        },
    }
    if field is None:
        cfg = make_config(cfg_overrides, remove=("openstack",))
        assert cfg.network.external.ingress_pool == "203.0.113.20-203.0.113.40"
    else:
        with pytest.raises(ConfigError, match=field):
            make_config(cfg_overrides, remove=("openstack",))


def test_pool_freeform_keys_are_preserved(make_config):
    """`tags` (label maps) and `config_patches` (freeform YAML) stay accepted."""
    cfg = make_config({
        "tags": {"team": "platform", "x": "y"},
        "workers": {"worker": {"count": 1, "flavor": "f", "disk": 20,
                               "tags": {"workload": "gpu"},
                               "config_patches": ["machine:\n  sysctls:\n    x: y"]}},
    })
    assert cfg.tags == {"team": "platform", "x": "y"}
    assert cfg.machines["testcluster-worker-01"].tags["workload"] == "gpu"


@pytest.mark.parametrize(
    ("secrets", "field"),
    [
        ({"openstack": {"credential_id": "a", "regoin": "x"}},
         r"openstack: unknown key\(s\): regoin"),
        ({"tailscale": {"auth_key": "x", "authky": "y"}},
         r"tailscale: unknown key\(s\): authky"),
    ],
)
def test_unknown_nested_secrets_key_is_rejected(make_config, tmp_path, secrets, field):
    """A miscapped key inside a secrets.yaml section is refused like any other."""
    _write_secrets(tmp_path, secrets)
    with pytest.raises(ConfigError, match=field):
        make_config()


# ---------------------------------------------------------------------------
# network.cluster / network.external L2 blocks
# ---------------------------------------------------------------------------

def _proxmox_new_network() -> dict:
    """A Proxmox cluster whose L2 facts live in the new `network` blocks."""
    return {
        "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
        "proxmox": {
            "url": "https://pve.example:8006",
            "storage": "vms",
            "iso_storage": "isos",
            "network": {"cluster": {"bridge": "vmbr0"}, "external": {"bridge": "vmbr1"}},
        },
        "network": {
            "cluster": {
                "cidr": "192.168.0.0/21",
                "gateway": "192.168.0.1",
                "vlan": 21,
                "mtu": 9000,
            },
            "external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "vlan": 1691,
                "kubeapi_vip": "203.0.113.10",
                "anchor_cidr": "169.254.32.0/20",
                "ingress_pool": "203.0.113.20-203.0.113.40",
            },
        },
    }


def test_network_blocks_are_parsed(make_config):
    cfg = make_config(_proxmox_new_network(), remove=("openstack",))

    assert cfg.network.dns == ["1.1.1.1"]
    assert cfg.network.ntp == ["ntp.example.com"]
    assert cfg.network.cluster.cidr == "192.168.0.0/21"
    assert cfg.network.cluster.gateway == "192.168.0.1"
    assert cfg.network.cluster.vlan == 21
    assert cfg.network.cluster.mtu == 9000
    assert cfg.network.cluster.kubeapi_vip == ""
    assert cfg.network.external is not None
    assert cfg.network.external.cidr == "203.0.113.0/24"
    assert cfg.network.external.vlan == 1691
    assert cfg.network.external.kubeapi_vip == "203.0.113.10"
    assert cfg.network.external.anchor_cidr == "169.254.32.0/20"
    assert cfg.network.external.ingress_pool == "203.0.113.20-203.0.113.40"


def test_network_block_mtu_defaults_to_1500(make_config):
    cfg = make_config()

    assert cfg.network.cluster.cidr == "192.168.0.0/21"
    assert cfg.network.cluster.mtu == 1500
    assert cfg.network.cluster.vlan is None
    assert cfg.network.external is None


def test_missing_cluster_cidr_is_rejected(make_config):
    with pytest.raises(ConfigError, match="network.cluster.cidr must be an IPv4 CIDR"):
        make_config(remove=("network.cluster.cidr",))


@pytest.mark.parametrize("key", ["anchor_cidr", "ingress_pool"])
def test_external_only_keys_are_rejected_under_cluster(make_config, key):
    values = {"anchor_cidr": "169.254.32.0/20", "ingress_pool": "192.168.0.20-192.168.0.40"}
    with pytest.raises(ConfigError, match="set them under network.external"):
        make_config({"network": {"cluster": {key: values[key]}}})


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"cidr": "not-a-cidr"}, "network.cluster.cidr"),
        ({"cidr": "192.168.0.5/21"}, "network.cluster.cidr"),
        ({"gateway": "10.0.0.1"}, "gateway must be inside network.cluster.cidr"),
        ({"gateway": "nope"}, "network.cluster.gateway is invalid"),
        ({"vlan": 4095}, "network.cluster.vlan must be 1-4094"),
        ({"vlan": "many"}, "'vlan' must be an integer"),
        ({"mtu": 1000}, "network.cluster.mtu must be 1280 or greater"),
        ({"mtu": "jumbo"}, "'mtu' must be an integer"),
        ({"kubeapi_vip": "203.0.113.10"}, "kubeapi_vip must be inside network.cluster.cidr"),
        ({"unknwn": 1}, r"network.cluster: unknown key\(s\): unknwn"),
    ],
)
def test_network_cluster_block_rejects_invalid_fields(make_config, block, message):
    overrides = {"network": {"cluster": {"cidr": "192.168.0.0/21", **block}}}
    with pytest.raises(ConfigError, match=message):
        make_config(overrides)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"anchor_cidr": "10.0.0.0/24"}, "anchor_cidr must be inside 169.254.0.0/16"),
        ({"kubeapi_vip": "192.168.0.10"}, "kubeapi_vip must be inside network.external.cidr"),
        ({"ingress_pool": "203.0.113.40-203.0.113.20"}, "start must be <= end"),
        ({"ingress_pool": "203.0.113.20"}, r"ingress_pool must be 'start-end'"),
        ({"ingress_pool": "10.0.0.1-10.0.0.9"},
         "ingress_pool must be inside network.external.cidr"),
        ({"unknwn": 1}, r"network.external: unknown key\(s\): unknwn"),
    ],
)
def test_network_external_block_rejects_invalid_fields(make_config, block, message):
    overrides = _proxmox_new_network()
    overrides["network"]["external"].update(block)
    with pytest.raises(ConfigError, match=message):
        make_config(overrides, remove=("openstack",))


def test_external_kubeapi_vip_inside_ingress_pool_is_rejected(make_config):
    overrides = _proxmox_new_network()
    overrides["network"]["external"]["kubeapi_vip"] = "203.0.113.30"
    with pytest.raises(ConfigError, match="must not be inside ingress_pool"):
        make_config(overrides, remove=("openstack",))


def test_kubeapi_vip_in_both_new_blocks_is_rejected(make_config):
    overrides = _proxmox_new_network()
    overrides["network"]["cluster"]["kubeapi_vip"] = "192.168.0.10"
    with pytest.raises(ConfigError, match="only one of network.cluster or network.external"):
        make_config(overrides, remove=("openstack",))


def test_kubeapi_vip_in_the_new_cluster_block_satisfies_proxmox(make_config):
    overrides = _proxmox_new_network()
    overrides["network"]["cluster"]["kubeapi_vip"] = "192.168.0.10"
    del overrides["network"]["external"]
    del overrides["proxmox"]["network"]["external"]

    cfg = make_config(overrides, remove=("openstack",))

    assert cfg.network.cluster.kubeapi_vip == "192.168.0.10"
    assert cfg.network.external is None


def test_proxmox_external_bridge_without_a_network_block_is_rejected(make_config):
    """The bridge alone does not describe the externally routed subnet."""
    with pytest.raises(ConfigError, match="needs a network.external block"):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": {"cluster": {"kubeapi_vip": "192.168.0.10"}},
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


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("cidr", "network.external.cidr must be an IPv4 CIDR"),
        ("gateway", "network.external.gateway must be an IPv4 address"),
        ("anchor_cidr", "network.external.anchor_cidr must be an IPv4 CIDR"),
    ],
)
def test_new_external_block_requires_the_full_definition(make_config, missing, message):
    overrides = _proxmox_new_network()
    del overrides["network"]["external"][missing]
    with pytest.raises(ConfigError, match=message):
        make_config(overrides, remove=("openstack",))


def test_new_external_block_requires_a_proxmox_bridge(make_config):
    overrides = _proxmox_new_network()
    del overrides["proxmox"]["network"]["external"]
    with pytest.raises(ConfigError, match="external.bridge"):
        make_config(overrides, remove=("openstack",))


def test_external_cidr_overlapping_the_cluster_cidr_is_rejected(make_config):
    overrides = _proxmox_new_network()
    overrides["network"]["external"].update(
        {"cidr": "192.168.0.0/24", "gateway": "192.168.0.1", "kubeapi_vip": "192.168.0.10"}
    )
    del overrides["network"]["external"]["ingress_pool"]
    with pytest.raises(ConfigError, match="must not overlap network.cluster.cidr"):
        make_config(overrides, remove=("openstack",))


def test_new_vlan_is_rejected_together_with_proxmox_sdn(make_config):
    with pytest.raises(ConfigError, match="mutually exclusive"):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "network": {"cluster": {"kubeapi_vip": "192.168.0.9", "vlan": 7}},
                "proxmox": {
                    "url": "https://pve.example:8006",
                    "storage": "vms",
                    "iso_storage": "isos",
                    "network": {"cluster": {"sdn": {}}},
                },
            },
            remove=("openstack",),
        )


def test_explicitly_null_l2_keys_are_treated_as_absent(make_config):
    cfg = make_config({"network": {"cluster": {"vlan": None, "mtu": None}}})

    assert cfg.network.cluster.vlan is None
    assert cfg.network.cluster.mtu == 1500


@pytest.mark.parametrize("attribute", ["cidr", "dns", "ntp"])
def test_network_facts_are_only_reachable_through_the_network_blocks(make_config, attribute):
    """The network settings read the way the config does, with no forwarders."""
    assert not hasattr(make_config(), attribute)


@pytest.mark.parametrize(
    ("section", "key", "moved_to"),
    [
        ("network", "cidr", "network.cluster.cidr"),
        ("cluster", "vlan", "network.cluster.vlan"),
        ("cluster", "kubeapi_vip", "network.cluster.kubeapi_vip"),
        ("external", "cidr", "network.external.cidr"),
        ("external", "gateway", "network.external.gateway"),
        ("external", "anchor_cidr", "network.external.anchor_cidr"),
        ("external", "kubeapi_vip", "network.external.kubeapi_vip"),
        ("external", "vlan", "network.external.vlan"),
        ("external", "ingress_pool", "network.external.ingress_pool"),
    ],
)
def test_old_network_key_is_rejected_with_its_new_location(make_config, section, key, moved_to):
    """Every key that moved names its new home instead of failing obscurely."""
    values = {
        "cidr": "203.0.113.0/24",
        "gateway": "203.0.113.1",
        "anchor_cidr": "169.254.40.0/24",
        "kubeapi_vip": "203.0.113.10",
        "vlan": 100,
        "ingress_pool": "203.0.113.20-203.0.113.40",
    }
    overrides = _proxmox_new_network()
    if section == "network":
        overrides["network"][key] = values[key]
        where = "network"
    else:
        overrides["proxmox"]["network"][section][key] = values[key]
        where = f"proxmox.network.{section}"
    with pytest.raises(ConfigError, match=f"{where}.{key} has moved to {moved_to}"):
        make_config(overrides, remove=("openstack",))


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (
            {"external": {
                "cidr": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "anchor_cidr": "169.254.40.0/24",
            }},
            "network.external is not valid with openstack",
        ),
        (
            {"cluster": {"kubeapi_vip": "192.168.0.10"}},
            "network.cluster.kubeapi_vip is not valid with openstack",
        ),
        ({"cluster": {"vlan": 100}}, "network.cluster.vlan is not valid with openstack"),
    ],
)
def test_proxmox_only_network_keys_are_rejected_on_openstack(make_config, block, message):
    """OpenStack builds its own external network, VIP and ingress at converge."""
    with pytest.raises(ConfigError, match=message):
        make_config({"network": block})


# The cluster.yaml of a real pre-redesign Proxmox cluster (the csfarm build),
# with placeholder addresses: the L2 facts live under `proxmox.network.*` and
# `network.cidr`. `charts:` is omitted -- it is plugin-owned and the plugin is
# not installed in the test environment.
_PRE_REDESIGN_CLUSTER_YAML = """\
name: farmcluster

talos:
  version: v1.13.10
  config_patches:
    - |
      machine:
        env:
          http_proxy: "http://proxy.example.edu:3128"
          https_proxy: "http://proxy.example.edu:3128"
          no_proxy: "localhost,127.0.0.1,10.0.0.0/8,192.168.100.0/24,169.254.0.0/16,.svc"
          HTTP_PROXY: "http://proxy.example.edu:3128"
          HTTPS_PROXY: "http://proxy.example.edu:3128"
          NO_PROXY: "localhost,127.0.0.1,10.0.0.0/8,192.168.100.0/24,169.254.0.0/16,.svc"
    - |
      apiVersion: v1alpha1
      kind: RoutingRuleConfig
      name: 0500
      dst: 203.0.113.0/24
      table: 100
kubernetes:
  version: v1.36.4

controlplane:
  count: 3
  cores: 4
  memory: 8
  disk: 40

workers:
  worker:
    count: 2
    cores: 16
    memory: 64
    disk: 100

proxmox:
  url: https://pve.example.edu:8006
  storage: vms
  iso_storage: isos
  cidata_storage: local
  placement_strategy: spread
  network:
    cluster:
      bridge: vmbr0
      kubeapi_vip: 192.168.100.200
    external:
      bridge: vmbr0
      vlan: 1691
      cidr: 203.0.113.0/24
      gateway: 203.0.113.1
      anchor_cidr: 169.254.32.0/20
      ingress_pool: 203.0.113.190-203.0.113.199

network:
  cidr: 192.168.100.0/24
  dns:
    - 192.0.2.2
    - 192.0.2.3
  ntp:
    - 192.0.2.2

security:
  kubernetes:
    operator: 203.0.113.17/32
  talos:
    operator: 203.0.113.17/32
"""

# The same cluster rewritten to the new shape: the L2 facts moved into the
# `network.cluster` / `network.external` blocks and `proxmox.network.*` keeps
# only the plumbing.
_REDESIGNED_CLUSTER_YAML = """\
name: farmcluster

talos:
  version: v1.13.10
  config_patches:
    - |
      machine:
        env:
          http_proxy: "http://proxy.example.edu:3128"
          https_proxy: "http://proxy.example.edu:3128"
          no_proxy: "localhost,127.0.0.1,10.0.0.0/8,192.168.100.0/24,169.254.0.0/16,.svc"
          HTTP_PROXY: "http://proxy.example.edu:3128"
          HTTPS_PROXY: "http://proxy.example.edu:3128"
          NO_PROXY: "localhost,127.0.0.1,10.0.0.0/8,192.168.100.0/24,169.254.0.0/16,.svc"
    - |
      apiVersion: v1alpha1
      kind: RoutingRuleConfig
      name: 0500
      dst: 203.0.113.0/24
      table: 100
kubernetes:
  version: v1.36.4

controlplane:
  count: 3
  cores: 4
  memory: 8
  disk: 40

workers:
  worker:
    count: 2
    cores: 16
    memory: 64
    disk: 100

proxmox:
  url: https://pve.example.edu:8006
  storage: vms
  iso_storage: isos
  cidata_storage: local
  placement_strategy: spread
  network:
    cluster:
      bridge: vmbr0
    external:
      bridge: vmbr0

network:
  cluster:
    cidr: 192.168.100.0/24
    kubeapi_vip: 192.168.100.200
  external:
    vlan: 1691
    cidr: 203.0.113.0/24
    gateway: 203.0.113.1
    anchor_cidr: 169.254.32.0/20
    ingress_pool: 203.0.113.190-203.0.113.199
  dns:
    - 192.0.2.2
    - 192.0.2.3
  ntp:
    - 192.0.2.2

security:
  kubernetes:
    operator: 203.0.113.17/32
  talos:
    operator: 203.0.113.17/32
"""

_PROXMOX_SECRETS_YAML = """\
proxmox:
  token_id: "root@pam!taloscluster"
  token_secret: "01234567-89ab-cdef-0123-456789abcdef"
"""


def _write_cluster_files(tmp_path: Path, cluster_yaml: str) -> None:
    (tmp_path / "cluster.yaml").write_text(cluster_yaml)
    (tmp_path / SECRETS_FILE).write_text(_PROXMOX_SECRETS_YAML)


def test_a_pre_redesign_cluster_yaml_fails_with_the_new_location_error(tmp_path):
    """The whole pre-redesign file -- every L2 fact still under
    `proxmox.network.*` plus `network.cidr` -- is refused naming the new home,
    so `plan` against it stops before touching anything."""
    _write_cluster_files(tmp_path, _PRE_REDESIGN_CLUSTER_YAML)

    with pytest.raises(
        ConfigError, match="cluster.yaml: network.cidr has moved to network.cluster.cidr"
    ):
        load_config(tmp_path)


def test_the_rewritten_cluster_yaml_loads_with_the_same_facts(tmp_path):
    """The rewritten file loads and every L2 fact sits in its new block, with
    the pools, patches and plumbing carried over unchanged -- the shape `plan`
    accepts."""
    _write_cluster_files(tmp_path, _REDESIGNED_CLUSTER_YAML)
    cfg = load_config(tmp_path)

    assert cfg.name == "farmcluster"
    assert cfg.network.cluster.cidr == "192.168.100.0/24"
    assert cfg.network.cluster.kubeapi_vip == "192.168.100.200"
    assert cfg.network.cluster.gateway == ""
    assert cfg.network.cluster.mtu == 1500
    assert cfg.network.external is not None
    assert cfg.network.external.vlan == 1691
    assert cfg.network.external.cidr == "203.0.113.0/24"
    assert cfg.network.external.gateway == "203.0.113.1"
    assert cfg.network.external.anchor_cidr == "169.254.32.0/20"
    assert cfg.network.external.ingress_pool == "203.0.113.190-203.0.113.199"
    assert cfg.network.external.kubeapi_vip == ""
    assert cfg.network.dns == ["192.0.2.2", "192.0.2.3"]
    assert cfg.network.ntp == ["192.0.2.2"]
    assert cfg.provider.network == {
        "cluster": {"bridge": "vmbr0"},
        "external": {"bridge": "vmbr0"},
    }
    assert sorted(cfg.machines) == [
        "farmcluster-controlplane-01",
        "farmcluster-controlplane-02",
        "farmcluster-controlplane-03",
        "farmcluster-worker-01",
        "farmcluster-worker-02",
    ]
    assert len(cfg.machines["farmcluster-worker-01"].config_patches) == 2


# ---------------------------------------------------------------------------
# include: extra YAML files merged into cluster.yaml
# ---------------------------------------------------------------------------

def test_include_merges_an_extra_file(make_config, tmp_path):
    """A pool defined in an included file loads as if it were in cluster.yaml."""
    (tmp_path / "pools.yaml").write_text(
        yaml.safe_dump({"workers": {"gpu": {"count": 1, "flavor": "g", "disk": 50}}})
    )
    cfg = make_config({"include": ["pools.yaml"]})

    assert "testcluster-gpu-01" in cfg.machines
    assert cfg.machines["testcluster-gpu-01"].flavor == "g"


def test_include_merges_into_an_existing_section(make_config, tmp_path):
    """Mappings merge key by key; the files need not own whole sections."""
    (tmp_path / "tags.yaml").write_text(yaml.safe_dump({"tags": {"team": "platform"}}))
    cfg = make_config({"include": ["tags.yaml"], "tags": {"site": "ncsa"}})

    assert cfg.tags == {"site": "ncsa", "team": "platform"}


def test_include_rejects_a_value_set_in_two_files(make_config, tmp_path):
    (tmp_path / "extra.yaml").write_text(yaml.safe_dump({"network": {"ntp": ["a"]}}))
    with pytest.raises(
        ConfigError, match="network.ntp is set in both cluster.yaml and extra.yaml"
    ):
        make_config({"include": ["extra.yaml"]})


def test_include_rejects_a_nested_include(make_config, tmp_path):
    (tmp_path / "extra.yaml").write_text(yaml.safe_dump({"include": ["more.yaml"]}))
    with pytest.raises(ConfigError, match="extra.yaml: include is only allowed"):
        make_config({"include": ["extra.yaml"]})


def test_include_rejects_an_unknown_key_naming_the_file(make_config, tmp_path):
    (tmp_path / "extra.yaml").write_text(yaml.safe_dump({"netwrok": {}}))
    with pytest.raises(ConfigError, match=r"extra.yaml: unknown key\(s\): netwrok"):
        make_config({"include": ["extra.yaml"]})


def test_include_rejects_a_missing_file(make_config):
    with pytest.raises(ConfigError, match="missing .*gone.yaml"):
        make_config({"include": ["gone.yaml"]})


@pytest.mark.parametrize(
    ("include", "message"),
    [
        ("metal.yaml", "include must be a list"),
        ([""], "include entries must be non-empty"),
        ([7], "include entries must be non-empty"),
        (["/etc/passwd"], "must be a path inside the cluster directory"),
        (["../other/cluster.yaml"], "must be a path inside the cluster directory"),
    ],
)
def test_include_rejects_invalid_entries(make_config, include, message):
    with pytest.raises(ConfigError, match=message):
        make_config({"include": include})


def test_include_conflict_between_two_included_files_names_both(make_config, tmp_path):
    """A collision deep inside a section blames the two files that set it."""
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"tags": {"t": "one"}}))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({"tags": {"t": "two"}}))
    with pytest.raises(ConfigError, match="tags.t is set in both a.yaml and b.yaml"):
        make_config({"include": ["a.yaml", "b.yaml"]})


def test_include_rejects_the_same_file_twice(make_config, tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"tags": {"t": "one"}}))
    with pytest.raises(ConfigError, match="include lists a.yaml twice"):
        make_config({"include": ["a.yaml", "a.yaml"]})


def test_include_rejects_a_symlink_out_of_the_cluster_directory(make_config, tmp_path):
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text(yaml.safe_dump({"tags": {"t": "one"}}))
    (tmp_path / "link.yaml").symlink_to(outside)
    with pytest.raises(ConfigError, match="must be a path inside the cluster directory"):
        make_config({"include": ["link.yaml"]})


def test_include_rejects_a_directory(make_config, tmp_path):
    (tmp_path / "conf").mkdir()
    with pytest.raises(ConfigError, match="include conf is not a file"):
        make_config({"include": ["conf"]})


def test_include_treats_an_explicit_null_section_as_absent(make_config, tmp_path):
    (tmp_path / "ts.yaml").write_text(
        yaml.safe_dump({"tailscale": {"login_server": "https://hs.example"}})
    )
    cfg = make_config({"include": ["ts.yaml"], "tailscale": None})

    assert cfg.login_server == "https://hs.example"


def test_credentials_stay_out_of_the_config_repr(make_config, tmp_path):
    """A traceback or debug print of the config must not leak a credential."""
    _write_secrets(
        tmp_path,
        {"openstack": {"credential_id": "id", "credential_secret": "super-secret"},
         "tailscale": {"auth_key": "tskey-secret"}},
    )
    cfg = make_config()

    assert "super-secret" not in repr(cfg)
    assert "tskey-secret" not in repr(cfg)
    assert "super-secret" not in repr(cfg.provider)
    # the values are still there for the commands that need them
    assert cfg.openstack_credentials == ("id", "super-secret")


def test_a_tailscale_section_only_in_secrets_does_not_enable_tailscale(make_config, tmp_path):
    """secrets.yaml holds credentials, not the decision to run tailscale."""
    _write_secrets(
        tmp_path,
        {"openstack": dict(OPENSTACK_CREDENTIALS), "tailscale": {"auth_key": "tskey-x"}},
    )
    cfg = make_config()

    assert cfg.tailscale_enabled is False
    for machine in cfg.machines.values():
        assert "siderolabs/tailscale" not in machine.extensions


def test_a_tailscale_section_in_an_include_enables_tailscale(make_config, tmp_path):
    (tmp_path / "ts.yaml").write_text(
        yaml.safe_dump({"tailscale": {"login_server": "https://hs.example"}})
    )
    _write_secrets(
        tmp_path,
        {"openstack": dict(OPENSTACK_CREDENTIALS), "tailscale": {"auth_key": "tskey-x"}},
    )
    cfg = make_config({"include": ["ts.yaml"]})

    assert cfg.tailscale_enabled is True
    assert cfg.tailscale_auth_key == "tskey-x"
    for machine in cfg.machines.values():
        assert "siderolabs/tailscale" in machine.extensions


def test_include_may_not_list_secrets_yaml(make_config, tmp_path):
    _write_secrets(tmp_path, {"openstack": dict(OPENSTACK_CREDENTIALS)})
    with pytest.raises(ConfigError, match="secrets.yaml is always included"):
        make_config({"include": ["secrets.yaml"]})
