"""Tests for taloscluster.config: machines expansion, extension/patch resolution,
validation errors, warnings, and cached_property semantics."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster.config import (
    ConfigError,
    OpenStackConfig,
    OpenStackSecrets,
    ProxmoxConfig,
    ProxmoxSecrets,
    SecurityRule,
    load_secrets,
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


# ---------------------------------------------------------------------------
# extension resolution
# ---------------------------------------------------------------------------

def test_extensions_base_always_present(make_config):
    cfg = make_config()
    for m in cfg.machines.values():
        assert set(BASE_EXTENSIONS).issubset(set(m.extensions))
        # sorted + deduped tuple
        assert m.extensions == tuple(sorted(set(m.extensions)))


def test_extensions_cluster_and_pool_merged(make_config):
    cfg = make_config({
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
        ({"network": {"cidr": "not-a-cidr"}}, "network.cidr"),
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


def test_exactly_one_provider_is_required(make_config):
    with pytest.raises(ConfigError, match="exactly one.*openstack.*proxmox"):
        make_config(remove=("openstack",))

    with pytest.raises(ConfigError, match="exactly one.*openstack.*proxmox"):
        make_config({"proxmox": {"url": "https://pve.example"}})


def test_proxmox_provider_section_is_typed(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 3, "cores": 4, "memory": 8, "disk": 40},
            "workers": {
                "worker": {"count": 1, "cores": 8, "memory": 16, "disk": 100}
            },
            "proxmox": {
                "url": "https://pve.example:8006",
                "storage": "vms",
                "iso_storage": "isos",
                "cidata_storage": "local",
                "placement_strategy": "spread",
                "nodes": ["pve001", "pve002"],
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
    ("proxmox", "message"),
    [
        (
            {
                "url": "https://pve.example",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "kubeapi_vip": "192.168.0.10"}},
            },
            "proxmox.storage",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "vnet": "talos"}},
            },
            "exactly one bridge or vnet",
        ),
        (
            {
                "url": "https://pve.example",
                "storage": "vms",
                "iso_storage": "isos",
                "network": {"cluster": {"bridge": "vmbr0", "kubeapi_vip": "203.0.113.10"}},
            },
            "inside network.cidr",
        ),
    ],
)
def test_proxmox_compute_configuration_is_validated(make_config, proxmox, message):
    with pytest.raises(ConfigError, match=message):
        make_config(
            {
                "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
                "proxmox": proxmox,
            },
            remove=("openstack",),
        )


def _proxmox_external_overrides() -> dict:
    """A valid proxmox.network.external section that passes validation."""
    return {
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
    }


def test_proxmox_external_section_validates(make_config):
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            **_proxmox_external_overrides(),
        },
        remove=("openstack",),
    )
    assert cfg.provider.network["external"]["kubeapi_vip"] == "203.0.113.10"


def test_proxmox_external_makes_cluster_kubeapi_vip_optional(make_config):
    # cluster section has only bridge, no kubeapi_vip — valid when external is present
    cfg = make_config(
        {
            "controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40},
            **_proxmox_external_overrides(),
        },
        remove=("openstack",),
    )
    assert "kubeapi_vip" not in cfg.provider.network["cluster"]


def test_proxmox_vip_can_be_in_cluster_with_external_present(make_config):
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
    assert cfg.provider.network["cluster"]["kubeapi_vip"] == "192.168.0.10"
    assert "kubeapi_vip" not in cfg.provider.network.get("external", {})


def test_proxmox_vip_rejected_when_in_both_sections(make_config):
    with pytest.raises(ConfigError, match="only one of network.cluster or network.external"):
        make_config(
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
                            "kubeapi_vip": "203.0.113.10",
                        },
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
                        },
                    },
                },
            },
            remove=("openstack",),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bridge": ""}, "external.bridge"),
        ({"cidr": ""}, "external.cidr"),
        ({"cidr": "192.168.0.0/24"}, "must not overlap network.cidr"),
        ({"gateway": "10.0.0.1"}, "external.gateway.*inside external.cidr"),
        ({"anchor_cidr": "10.0.0.0/24"}, "anchor_cidr.*169.254.0.0/16"),
        ({"kubeapi_vip": "192.168.0.10"}, "external.kubeapi_vip.*inside external.cidr"),
        ({"ingress_pool": "203.0.113.50-203.0.113.10"}, "start must be <= end"),
        ({"ingress_pool": "203.0.113.50-999.999.999.999"}, "invalid addresses"),
    ],
)
def test_proxmox_external_section_rejects_invalid_fields(make_config, overrides, message):
    base = _proxmox_external_overrides()
    base["proxmox"]["network"]["external"].update(overrides)
    with pytest.raises(ConfigError, match=message):
        make_config(
            {"controlplane": {"count": 1, "cores": 4, "memory": 8, "disk": 40}, **base},
            remove=("openstack",),
        )


def _write_provider_files(root: Path, cluster: dict, secrets: dict) -> None:
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster))
    (root / "secrets.yaml").write_text(yaml.safe_dump(secrets))


def test_openstack_secrets_are_typed_and_existing_fields_remain(tmp_path):
    cluster = {
        "openstack": {
            "url": "https://example.com/v3",
            "availability_zone": "nova",
            "external_net": "public",
        }
    }
    _write_provider_files(
        tmp_path,
        cluster,
        {"openstack": {"credential_id": "id", "credential_secret": "secret"}},
    )

    secrets = load_secrets(tmp_path)

    assert isinstance(secrets.provider, OpenStackSecrets)
    assert secrets.openstack_credential_id == "id"
    assert secrets.openstack_credential_secret == "secret"


def test_proxmox_secrets_are_typed(tmp_path):
    _write_provider_files(
        tmp_path,
        {"proxmox": {"url": "https://pve.example"}},
        {"proxmox": {"token_id": "user@pve!provider", "token_secret": "secret"}},
    )

    secrets = load_secrets(tmp_path)

    assert isinstance(secrets.provider, ProxmoxSecrets)
    assert secrets.provider.token_id == "user@pve!provider"


def test_secrets_provider_must_match_cluster_provider(tmp_path):
    _write_provider_files(
        tmp_path,
        {"proxmox": {"url": "https://pve.example"}},
        {"openstack": {"credential_id": "id", "credential_secret": "secret"}},
    )

    with pytest.raises(ConfigError, match="proxmox.*credentials"):
        load_secrets(tmp_path)


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
