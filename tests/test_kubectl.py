"""The kubectl wrapper applies a wall-clock bound to every subprocess so a
kube-api that accepts TCP but never answers cannot hang converge, and a caller
can tell a timed-out (hung) request apart from an abrupt negative answer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from taloscluster.k8s import kubectl


class _Proc:
    returncode = 0
    stdout = "{}"
    stderr = ""


@pytest.mark.parametrize(
    "call",
    [
        lambda kc: kubectl.cluster_up(kc),
        lambda kc: kubectl.node_names(kc),
        lambda kc: kubectl.node_exists(kc, "cp-01"),
        lambda kc: kubectl.node_ready(kc, "cp-01"),
        lambda kc: kubectl.server_version(kc),
        lambda kc: kubectl.get_nodes_wide(kc),
        lambda kc: kubectl.node_summary(kc),
        lambda kc: kubectl.unschedulable(kc),
        lambda kc: kubectl.uncordon(kc, "cp-01"),
        lambda kc: kubectl.delete_node(kc, "cp-01"),
    ],
)
def test_every_read_is_bounded(monkeypatch, tmp_path, call):
    """Every kubectl call routes through _run's wall-clock bound."""
    captured = {}

    def run(args, **kw):
        captured["timeout"] = kw.get("timeout")
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr(kubectl.subprocess, "run", run)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("clusters: []\n")
    call(kubeconfig)
    assert captured["timeout"] == kubectl.RUN_TIMEOUT
    assert captured["args"][0] == "kubectl"


def test_drain_uses_a_longer_wall_clock_bound(monkeypatch, tmp_path):
    captured = {}

    def run(args, **kw):
        captured["timeout"] = kw.get("timeout")
        return _Proc()

    monkeypatch.setattr(kubectl.subprocess, "run", run)
    monkeypatch.setattr(kubectl, "action", lambda _m: None)
    monkeypatch.setattr(kubectl, "dry_run", lambda: False)
    kubectl.drain(tmp_path / "kubeconfig", "cp-01")
    assert captured["timeout"] == kubectl.DRAIN_TIMEOUT


def test_run_propagates_a_timeout(monkeypatch):
    def hung(*_a, **_k):
        raise subprocess.TimeoutExpired("kubectl", kubectl.RUN_TIMEOUT)

    monkeypatch.setattr(kubectl.subprocess, "run", hung)
    with pytest.raises(subprocess.TimeoutExpired):
        kubectl._run(["kubectl"], check=False)


def test_unschedulable_does_not_collapse_a_timeout_into_empty(monkeypatch):
    def hung(*_a, **_k):
        raise subprocess.TimeoutExpired("kubectl", kubectl.RUN_TIMEOUT)

    monkeypatch.setattr(kubectl, "_run", hung)
    with pytest.raises(subprocess.TimeoutExpired):
        kubectl.unschedulable(Path("kubeconfig"))


def test_unschedulable_still_returns_empty_on_a_negative_answer(monkeypatch):
    proc = _Proc()
    proc.returncode = 1
    monkeypatch.setattr(kubectl, "_run", lambda *a, **k: proc)
    assert kubectl.unschedulable(Path("kubeconfig")) == []


def test_node_ready_does_not_collapse_a_timeout_into_unknown(monkeypatch):
    def hung(*_a, **_k):
        raise subprocess.TimeoutExpired("kubectl", kubectl.RUN_TIMEOUT)

    monkeypatch.setattr(kubectl, "_run", hung)
    with pytest.raises(subprocess.TimeoutExpired):
        kubectl.node_ready(Path("kubeconfig"), "cp-01")


def test_node_ready_still_returns_none_on_a_negative_answer(monkeypatch):
    proc = _Proc()
    proc.returncode = 1
    monkeypatch.setattr(kubectl, "_run", lambda *a, **k: proc)
    assert kubectl.node_ready(Path("kubeconfig"), "cp-01") is None


def test_server_version_does_not_collapse_a_timeout_into_empty(monkeypatch):
    def hung(*_a, **_k):
        raise subprocess.TimeoutExpired("kubectl", kubectl.RUN_TIMEOUT)

    monkeypatch.setattr(kubectl, "_run", hung)
    with pytest.raises(subprocess.TimeoutExpired):
        kubectl.server_version(Path("kubeconfig"))


def test_server_version_still_returns_empty_on_a_negative_answer(monkeypatch):
    proc = _Proc()
    proc.returncode = 1
    monkeypatch.setattr(kubectl, "_run", lambda *a, **k: proc)
    assert kubectl.server_version(Path("kubeconfig")) == ""
