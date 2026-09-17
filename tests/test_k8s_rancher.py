"""The shared Rancher cluster-id reader under `taloscluster/k8s`.

Both the rancher plugin and the standalone argocd plugin resolve the downstream
cluster's Rancher cluster id from the cattle-cluster-agent credentials Secret,
so the parsing lives once here rather than drifting across two copies.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from taloscluster.k8s import rancher


def _proc(returncode, stdout=""):
    class Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout

    return Proc()


def _secret(name, namespace):
    return {
        "metadata": {"name": name},
        "data": {"namespace": base64.b64encode(namespace.encode()).decode()},
    }


def test_cluster_id_reads_the_credentials_namespace(monkeypatch):
    payload = json.dumps({
        "items": [_secret("cattle-credentials-abc", "c-abc12")],
    })
    calls = []
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda args, **kw: calls.append(args) or _proc(0, payload),
    )
    assert rancher.cluster_id(Path("/root/kubeconfig")) == "c-abc12"
    assert "--kubeconfig" in calls[0]
    assert "/root/kubeconfig" in calls[0]


def test_cluster_id_is_none_when_kubectl_fails(monkeypatch):
    # A failed kubectl call means no agent (cattle-system absent, cluster down);
    # there is no Rancher identity to act on.
    monkeypatch.setattr(rancher.kubectl, "_run", lambda *a, **k: _proc(1))
    assert rancher.cluster_id(Path("kubeconfig")) is None


def test_cluster_id_is_none_without_a_credentials_secret(monkeypatch):
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(0, json.dumps({"items": [
            _secret("some-other-secret", "c-abc12"),
            {"metadata": {"name": "cattle-credentials-x"}, "data": {}},
        ]})),
    )
    assert rancher.cluster_id(Path("kubeconfig")) is None


def test_cluster_id_is_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(
        rancher.kubectl, "_run", lambda *a, **k: _proc(0, "not json"),
    )
    assert rancher.cluster_id(Path("kubeconfig")) is None


def test_cluster_id_is_none_on_unbase64able_namespace(monkeypatch):
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(0, json.dumps({"items": [
            {"metadata": {"name": "cattle-credentials-zz"},
             "data": {"namespace": "not-base64!!"}},
        ]})),
    )
    assert rancher.cluster_id(Path("kubeconfig")) is None


def test_rancher_plugin_delegates_to_the_shared_helper(monkeypatch):
    """`downstream_rancher_id` must call the shared helper, so the rancher
    plugin no longer carries its own copy of the secret parsing."""
    from taloscluster_rancher import reconcile as _converge

    called = []
    monkeypatch.setattr(_converge, "_kubectl", lambda root, *a: "cattle-system")
    monkeypatch.setattr(
        rancher, "cluster_id", lambda kc: called.append(kc) or "c-abc12",
    )
    assert _converge.downstream_rancher_id(Path("/root")) == "c-abc12"
    assert called == [Path("/root") / "kubeconfig"]


def test_rancher_plugin_keeps_the_namespace_pre_check(monkeypatch):
    """When cattle-system is absent the plugin returns None without consulting
    the shared helper."""
    from taloscluster_rancher import reconcile as _converge

    monkeypatch.setattr(_converge, "_kubectl", lambda root, *a: None)
    monkeypatch.setattr(rancher, "cluster_id",
                        lambda kc: pytest.fail("helper must not run"))
    assert _converge.downstream_rancher_id(Path("/root")) is None


def test_argocd_plugin_delegates_to_the_shared_helper(monkeypatch):
    """The argocd kube wrapper must call the shared helper too, so both plugins
    parse through one implementation."""
    from taloscluster_argocd import kube as argocd_kube

    called = []
    monkeypatch.setattr(rancher, "cluster_id", lambda kc: called.append(kc) or "c-abc12")
    assert argocd_kube.downstream_rancher_id(Path("/root")) == "c-abc12"
    assert called == [Path("/root") / "kubeconfig"]


def test_cluster_id_is_bounded(monkeypatch):
    """The shared reader routes through the kubectl wrapper's wall-clock bound, so
    a kube-api that accepts TCP but never answers cannot hang converge's check."""
    captured = {}

    def run(args, **kw):
        captured["timeout"] = kw.get("timeout")
        return _proc(0, json.dumps({"items": [_secret("cattle-credentials-a", "c-1")]}))

    monkeypatch.setattr(rancher.kubectl.subprocess, "run", run)
    assert rancher.cluster_id(Path("/root/kubeconfig")) == "c-1"
    assert captured["timeout"] == rancher.kubectl.RUN_TIMEOUT


def test_argocd_kubectl_calls_are_bounded(monkeypatch, tmp_path):
    """argocd's apply/get/delete/diff all run through kubectl._run's wall-clock
    bound, so its converge/check hooks cannot hang on a non-responsive api."""
    from taloscluster_argocd import kube as argocd_kube
    from taloscluster_argocd.config import ApplyTarget

    captured = {}
    target = ApplyTarget(kubeconfig="kubeconfig", context=None)
    (tmp_path / "kubeconfig").write_text("clusters: []\n")

    def run(args, **kw):
        captured["timeout"] = kw.get("timeout")
        captured["args"] = args
        return _proc(0)

    monkeypatch.setattr(argocd_kube.kubectl.subprocess, "run", run)
    monkeypatch.setattr(argocd_kube, "dry_run", lambda: False)
    monkeypatch.setattr(argocd_kube, "action", lambda _m: None)
    monkeypatch.setattr(argocd_kube, "info", lambda _l: None)

    argocd_kube.exists(target, tmp_path, "manifest")
    assert captured["timeout"] == argocd_kube.kubectl.RUN_TIMEOUT
    argocd_kube.matches(target, tmp_path, "manifest")
    assert captured["timeout"] == argocd_kube.kubectl.RUN_TIMEOUT
    argocd_kube.apply(target, tmp_path, "manifest")
    assert captured["timeout"] == argocd_kube.kubectl.RUN_TIMEOUT
    argocd_kube.delete(target, tmp_path, "manifest")
    assert captured["timeout"] == argocd_kube.kubectl.RUN_TIMEOUT
    argocd_kube.exists_downstream(tmp_path, "manifest")
    assert captured["timeout"] == argocd_kube.kubectl.RUN_TIMEOUT
