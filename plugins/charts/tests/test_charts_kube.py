"""kubectl helper bounds: hung api calls raise the plugin error, server-side apply."""

from __future__ import annotations

import subprocess

import pytest
from taloscluster import output
from taloscluster.errors import ReconcileError

from taloscluster_charts import kube


@pytest.fixture(autouse=True)
def _reset_dry_run():
    output.set_dry_run(False)
    yield
    output.set_dry_run(False)


def test_run_wraps_a_timeout(monkeypatch, tmp_path):
    """A kube-api that accepts TCP but never answers surfaces as a
    ReconcileError naming the command -- the error the plugin's per-entry
    handling and activation probe catch -- not a raw TimeoutExpired."""

    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 30))

    monkeypatch.setattr(kube.kubectl, "_run", run)
    with pytest.raises(ReconcileError, match=r"kubectl get -f manifest.yaml timed out"):
        kube.exists(tmp_path, "manifest.yaml")


def test_apply_goes_server_side_only_on_request(monkeypatch, tmp_path):
    """`server_side` appends --server-side; the default stays client-side."""
    captured: dict = {}

    def run(root, args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(kube, "_run", run)
    kube.apply(tmp_path, "-", label="test", input="")
    assert "--server-side" not in captured["args"]
    kube.apply(tmp_path, "-", label="test", input="", server_side=True)
    assert "--server-side" in captured["args"]
