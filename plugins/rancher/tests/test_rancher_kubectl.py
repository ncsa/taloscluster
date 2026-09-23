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


# ---- _remove_agent ----------------------------------------------------------

def _remove_agent_run(monkeypatch, procs):
    """Stub kubectl._run to answer with `procs` in order, recording the calls."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        return procs.pop(0)

    monkeypatch.setattr(reconcile.kubectl, "_run", run)
    monkeypatch.setattr(reconcile, "dry_run", lambda: False)
    monkeypatch.setattr(reconcile, "action", lambda _m: None)
    return calls


def _proc(returncode=0, stdout="", stderr=""):
    proc = type("Proc", (), {})()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_remove_agent_deletes_cattle_system_without_waiting(monkeypatch, tmp_path):
    """The namespace delete only has to be accepted by the kube-api: namespace
    finalizers drain asynchronously and can outrun any subprocess timeout, so
    the command carries `--wait=false`."""
    calls = _remove_agent_run(monkeypatch, [_proc(0), _proc(0, stdout="ns deleted")])
    reconcile._remove_agent(tmp_path)
    assert calls[0][-3:] == ["get", "ns", "cattle-system"]
    assert calls[1][-4:] == ["delete", "ns", "cattle-system", "--wait=false"]
    assert calls[1][0] == kubectl.BIN


def test_remove_agent_is_a_noop_without_cattle_system(monkeypatch, tmp_path):
    """No cattle-system, no agent to uninstall: only the probe runs."""
    calls = _remove_agent_run(monkeypatch, [_proc(1, stderr="not found")])
    reconcile._remove_agent(tmp_path)
    assert len(calls) == 1


def test_remove_agent_reports_a_failed_delete(monkeypatch, tmp_path):
    """A delete the kube-api rejects fails the destroy instead of being ignored."""
    _remove_agent_run(monkeypatch, [_proc(0), _proc(1, stderr="forbidden")])
    with pytest.raises(RancherError, match="uninstalling the Rancher agent failed.*forbidden"):
        reconcile._remove_agent(tmp_path)


def test_remove_agent_reports_a_hung_delete(monkeypatch, tmp_path):
    def run(args, **kw):
        if "delete" in args:
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))
        return _proc(0)

    monkeypatch.setattr(reconcile.kubectl, "_run", run)
    monkeypatch.setattr(reconcile, "dry_run", lambda: False)
    monkeypatch.setattr(reconcile, "action", lambda _m: None)
    with pytest.raises(RancherError, match="delete ns cattle-system --wait=false timed out"):
        reconcile._remove_agent(tmp_path)


def test_remove_agent_dry_run_only_previews(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(reconcile.kubectl, "_run",
                        lambda args, **kw: calls.append(args) or _proc(0))
    monkeypatch.setattr(reconcile, "dry_run", lambda: True)
    seen = []
    monkeypatch.setattr(reconcile, "action", seen.append)
    reconcile._remove_agent(tmp_path)
    # the read-only namespace probe runs, but no delete is issued
    assert len(calls) == 1
    assert seen == ["delete cattle-system namespace (uninstall Rancher agent) via kubectl"]
