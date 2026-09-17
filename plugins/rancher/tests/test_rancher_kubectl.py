"""The rancher plugin's kubectl helpers translate a hung kube-api into a clear
RancherError instead of leaking a raw TimeoutExpired out of check/status/destroy,
and manifest writes (the agent install apply) use the longer manifest bound."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taloscluster.k8s import kubectl

from taloscluster_rancher import reconcile
from taloscluster_rancher.errors import RancherError


class _Client:
    def __init__(self, command="import-command"):
        self._command = command

    def fetch_import_command(self, cluster):
        return self._command


@pytest.fixture
def wire(monkeypatch):
    def _install(*, raise_timeout, returncode=0):
        seen = {}

        def run(args, **kw):
            seen["args"] = args
            seen["timeout"] = kw.get("timeout")
            if raise_timeout:
                raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))
            proc = type("Proc", (), {})()
            proc.returncode = returncode
            proc.stdout = "out"
            proc.stderr = "err"
            return proc

        monkeypatch.setattr(reconcile.kubectl, "_run", run)
        monkeypatch.setattr(reconcile, "dry_run", lambda: False)
        monkeypatch.setattr(reconcile, "action", lambda _m: None)
        return seen

    return _install


def test_kubectl_helper_turns_a_timeout_into_rancher_error(wire):
    """A hung kube-api is not "cattle-system is absent"; it must be a real error,
    not a None that downstream_rancher_id would read as "no agent"."""
    seen = wire(raise_timeout=True)
    with pytest.raises(RancherError, match="timed out.*investigate"):
        reconcile._kubectl(Path("/root"), "get", "ns", "cattle-system")
    assert seen["args"][:1] == [kubectl.BIN]


def test_kubectl_helper_still_returns_none_on_a_negative_answer(wire):
    wire(raise_timeout=False, returncode=1)
    assert reconcile._kubectl(Path("/root"), "get", "ns", "cattle-system") is None


def test_install_agent_turns_a_timeout_into_rancher_error(wire):
    """The agent install apply only caught CalledProcessError; a hung apply must
    now raise a clear error, not a raw TimeoutExpired, and uses the manifest bound."""
    seen = wire(raise_timeout=True)
    cluster = type("C", (), {"id": "c-abc12"})()
    with pytest.raises(RancherError, match="Rancher import manifest.*timed out"):
        reconcile.install_agent(Path("/root"), _Client(), cluster)
    assert seen["timeout"] == kubectl.MANIFEST_TIMEOUT


def test_install_agent_uses_the_manifest_bound(wire):
    seen = wire(raise_timeout=False)
    cluster = type("C", (), {"id": "c-abc12"})
    reconcile.install_agent(Path("/root"), _Client(), cluster)
    assert seen["timeout"] == kubectl.MANIFEST_TIMEOUT
    assert seen["args"][-3:] == ["apply", "-f", "-"]
