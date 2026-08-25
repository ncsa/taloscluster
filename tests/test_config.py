"""Tests for taloscluster.config: machines expansion, extension/patch resolution,
validation errors, warnings, and cached_property semantics."""

from __future__ import annotations

import pytest

from taloscluster.config import ConfigError, validate_warnings
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
