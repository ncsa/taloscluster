"""helm list records and the release lookup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taloscluster.errors import ReconcileError

from taloscluster_charts import helm


def test_strips_release_prefix():
    assert helm.chart_version({"chart": "metallb-0.16.1"}) == "0.16.1"
    assert helm.chart_version({"chart": "traefik-41.6.0"}) == "41.6.0"
    assert helm.chart_version({"chart": "sealed-secrets-2.20.0"}) == "2.20.0"


def test_keeps_v_prefixed_versions():
    assert helm.chart_version({"chart": "cert-manager-v1.21.2"}) == "v1.21.2"
    assert helm.chart_version({"chart": "cert-manager-1.21.2"}) == "1.21.2"


def test_falls_back_to_chart_field():
    assert helm.chart_version({"chart": "weird"}) == "weird"
    assert helm.chart_version({}) == ""


def test_release_finds_pending_releases(monkeypatch):
    """`helm list` hides pending-* releases; --all surfaces them so converge
    sees the stuck state and clears the release instead of dying on helm's
    pending guard ("another operation is in progress")."""
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='[{"name": "metallb", "status": "pending-upgrade", "chart": "metallb-0.14.9"}]',
            stderr="",
        )

    monkeypatch.setattr(helm.subprocess, "run", run)
    record = helm.release(Path("kubeconfig"), "metallb", "metallb-system")
    assert "--all" in captured["args"]
    assert record == {"name": "metallb", "status": "pending-upgrade", "chart": "metallb-0.14.9"}


def test_release_ignores_uninstalled_history(monkeypatch):
    """A release uninstalled with --keep-history is absent as far as helm goes."""

    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='[{"name": "metallb", "status": "uninstalled", "chart": "metallb-0.14.9"}]',
            stderr="",
        )

    monkeypatch.setattr(helm.subprocess, "run", run)
    assert helm.release(Path("kubeconfig"), "metallb", "metallb-system") is None


def test_run_wraps_a_timeout(monkeypatch):
    """A helm command that hangs past its bound raises the plugin's
    ReconcileError naming the command -- the error converge's per-entry
    handling catches -- instead of a raw subprocess.TimeoutExpired."""

    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 30))

    monkeypatch.setattr(helm.subprocess, "run", run)
    with pytest.raises(ReconcileError, match=r"helm --kubeconfig .* list .*timed out"):
        helm.release(Path("kubeconfig"), "metallb", "metallb-system")
