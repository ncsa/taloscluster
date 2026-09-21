"""Tests for converge.check -- the version report and its exit code.

The upstream lookups and the cluster probes are monkeypatched, so these assert
the verdict logic (patch vs minor update, node drift, exit code) without any
network, cloud or talosctl/kubectl binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from taloscluster import converge, versions

CLUSTER = {
    "name": "testcluster",
    "talos": {"version": "v1.13.8"},
    "kubernetes": {"version": "v1.35.2"},
    "controlplane": {"count": 1, "flavor": "gp.medium", "disk": 40},
    "openstack": {
        "url": "https://example.com:5000/v3/",
        "availability_zone": "nova",
        "external_net": "ext-net",
    },
    "network": {"cluster": {"cidr": "192.168.0.0/21"}, "dns": ["1.1.1.1"],
                "ntp": ["ntp.example.com"]},
}

TALOS_VERSIONS = ["v1.13.8", "v1.13.9", "v1.14.0-rc.1"]


@pytest.fixture
def cluster_dir(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump(CLUSTER))
    return tmp_path


@pytest.fixture
def upstream(monkeypatch):
    """Fake the upstream lookups; returns a dict the test can retune."""
    state = {"talos": TALOS_VERSIONS, "k8s_latest": "v1.36.4", "k8s_patch": "v1.35.8"}
    monkeypatch.setattr(versions, "talos_versions", lambda: state["talos"])
    monkeypatch.setattr(versions, "latest_kubernetes", lambda: state["k8s_latest"])
    monkeypatch.setattr(
        versions,
        "latest_kubernetes_patch",
        lambda minor: state["k8s_patch"] if minor == "1.35" else "",
    )
    return state


@pytest.fixture
def nodes(monkeypatch):
    """Fake what the cluster reports; retune the list to exercise drift."""
    state: dict = {"nodes": []}
    monkeypatch.setattr(converge, "_running_versions", lambda root, cfg: state["nodes"])
    return state


def _report(capsys) -> dict:
    return yaml.safe_load(capsys.readouterr().out)


def test_updates_available_exit_1(cluster_dir, upstream, nodes, capsys):
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    talos, k8s = report["components"]
    assert talos["latest_patch"] == "v1.13.9"  # not the v1.14.0-rc.1
    assert talos["patch_available"] and not talos["minor_available"]
    # kubernetes: a newer patch of 1.35 AND a newer minor exist; both reported
    assert k8s["latest_patch"] == "v1.35.8"
    assert k8s["latest"] == "v1.36.4"
    assert k8s["patch_available"] and k8s["minor_available"]
    assert report["up_to_date"] is False
    assert rc == 1


def test_up_to_date_exit_0(cluster_dir, upstream, nodes, capsys):
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [
        {"name": "testcluster-controlplane-01", "talos": "v1.13.8", "kubernetes": "v1.35.2"}
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["up_to_date"] is True
    assert report["drift"] == []
    assert report["incomplete"] is False
    assert report["incomplete_reasons"] == []
    assert rc == 0


def test_node_behind_configured_is_drift(cluster_dir, upstream, nodes, capsys):
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [
        {"name": "cp-01", "talos": "v1.13.7", "kubernetes": "v1.35.2"},
        {"name": "cp-02", "talos": "v1.13.8", "kubernetes": "v1.35.2"},
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["drift"] == ["cp-01"]
    assert rc == 1


def test_unreachable_node_is_incomplete_not_drift(cluster_dir, upstream, nodes, capsys):
    """An empty version means "did not answer", not "wrong version": it is not
    drift, but it IS an incomplete check, so the command must not pass as clean."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [{"name": "cp-01", "talos": "", "kubernetes": ""}]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["drift"] == []
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == [
        "node cp-01: talos version unknown",
        "node cp-01: kubernetes version unknown",
    ]
    assert report["up_to_date"] is False
    assert rc == 1


def test_node_with_unknown_kubelet_is_incomplete(cluster_dir, upstream, nodes, capsys):
    """Talos answered but the kubelet did not join: kubernetes is unknown, so
    the check is incomplete even though talos is fine."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [{"name": "cp-01", "talos": "v1.13.8", "kubernetes": ""}]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["drift"] == []
    assert report["incomplete_reasons"] == ["node cp-01: kubernetes version unknown"]
    assert rc == 1


def test_cluster_unreachable_when_expected_is_incomplete(cluster_dir, upstream, nodes, capsys):
    """A cluster that should exist (talosconfig present) but reports no node at
    all is unverified and must not pass as clean."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    (cluster_dir / "talosconfig").write_text("dummy")
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == ["cluster unreachable; no node versions known"]
    assert rc == 1


def test_unreachable_cluster_with_only_kubeconfig_is_incomplete(
    cluster_dir, upstream, nodes, capsys
):
    """kubeconfig alone records that a cluster was set up, so when the Kubernetes
    API does not answer and no node versions come back the check must not pass
    as clean -- even without a talosconfig."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    (cluster_dir / "kubeconfig").write_text("dummy")
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == ["cluster unreachable; no node versions known"]
    assert rc == 1


def test_kubelet_build_suffix_is_not_drift(cluster_dir, upstream, nodes, capsys):
    """kubelet reports v1.35.2 for a v1.35.2 pin, sometimes with a build suffix."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [{"name": "cp-01", "talos": "v1.13.8", "kubernetes": "v1.35.2+talos"}]
    converge.check(cluster_dir, output="yaml")
    assert _report(capsys)["drift"] == []


def test_upstream_unreachable_reports_config_only(cluster_dir, nodes, monkeypatch, capsys):
    """A lookup failure must warn and still print, not raise, and must not pass
    as a clean check: we never confirmed what the newest releases are."""

    def boom(*a, **k):
        raise converge.requests.RequestException("no route to host")

    monkeypatch.setattr(versions, "talos_versions", boom)
    monkeypatch.setattr(versions, "latest_kubernetes", boom)
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    for c in report["components"]:
        assert c["latest"] == "" and not c["patch_available"] and not c["minor_available"]
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == [
        "talos: newest release unknown",
        "kubernetes: newest release unknown",
    ]
    # nothing could be verified, so the command fails the build rather than
    # silently passing an unverified cluster
    assert rc == 1


def test_cordoned_node_is_reported(cluster_dir, upstream, nodes, capsys):
    """A leftover cordon breaks every health check but is invisible in a version
    comparison, so `check` has to call it out (and fail)."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    nodes["nodes"] = [
        {"name": "cp-01", "talos": "v1.13.8", "kubernetes": "v1.35.2", "cordoned": True}
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["cordoned"] == ["cp-01"]
    assert report["drift"] == []  # versions are right; scheduling is not
    assert report["up_to_date"] is False
    assert rc == 1


def test_missing_configured_nodes_are_incomplete(cluster_dir, upstream, nodes, capsys):
    """An existing cluster that reports only some of its configured machines is
    incomplete: nodes missing from both Talos discovery and Kubernetes have
    unverifiable versions, so check must not pass as current."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    # three configured control planes; only one shows up in either source
    (cluster_dir / "cluster.yaml").write_text(
        yaml.safe_dump(
            {
                **CLUSTER,
                "controlplane": {"count": 3, "flavor": "gp.medium", "disk": 40},
            }
        )
    )
    (cluster_dir / "talosconfig").write_text("dummy")
    nodes["nodes"] = [
        {"name": "testcluster-controlplane-03", "talos": "v1.13.8", "kubernetes": "v1.35.2"}
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == [
        "node testcluster-controlplane-01 is missing from both Talos discovery and Kubernetes",
        "node testcluster-controlplane-02 is missing from both Talos discovery and Kubernetes",
    ]
    assert report["up_to_date"] is False
    assert rc == 1


def test_missing_metal_node_is_incomplete(cluster_dir, upstream, nodes, capsys):
    """A metal server of a `metal:` section is a configured machine like any VM:
    once the cluster exists, check must report a joined-cluster metal node that
    answers in neither Talos discovery nor Kubernetes as missing."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    (cluster_dir / "cluster.yaml").write_text(
        yaml.safe_dump(
            {
                **CLUSTER,
                "metal": {
                    "worker": {
                        "role": "worker",
                        "disk": "/dev/sda",
                        "servers": {
                            "rp001-worker": {
                                "interfaces": {
                                    "enp1s0f0": {"role": "cluster", "ip": "192.168.0.5/21"}
                                }
                            },
                        },
                    },
                },
            }
        )
    )
    (cluster_dir / "talosconfig").write_text("dummy")
    nodes["nodes"] = [
        {"name": "testcluster-controlplane-01", "talos": "v1.13.8", "kubernetes": "v1.35.2"}
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["incomplete"] is True
    assert report["incomplete_reasons"] == [
        "node rp001-worker is missing from both Talos discovery and Kubernetes"
    ]
    assert report["up_to_date"] is False
    assert rc == 1


def test_missing_machines_not_reported_before_creation(cluster_dir, upstream, nodes, capsys):
    """Before a cluster exists there is no talosconfig/kubeconfig, so check
    reports the pinned versions only and a node that never answered is expected
    rather than flagged as missing."""
    upstream["talos"] = ["v1.13.8"]
    upstream["k8s_latest"] = upstream["k8s_patch"] = "v1.35.2"
    (cluster_dir / "cluster.yaml").write_text(
        yaml.safe_dump(
            {
                **CLUSTER,
                "controlplane": {"count": 3, "flavor": "gp.medium", "disk": 40},
            }
        )
    )
    nodes["nodes"] = [
        {"name": "testcluster-controlplane-03", "talos": "v1.13.8", "kubernetes": "v1.35.2"}
    ]
    rc = converge.check(cluster_dir, output="yaml")
    report = _report(capsys)
    assert report["incomplete"] is False
    assert report["incomplete_reasons"] == []
    assert report["up_to_date"] is True
    assert rc == 0
