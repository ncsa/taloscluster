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

from taloscluster.errors import ReconcileError
from taloscluster.k8s import rancher


def _proc(returncode, stdout="", stderr=""):
    class Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    return Proc()


def _secret(name, namespace):
    return {
        "metadata": {"name": name},
        "data": {"namespace": base64.b64encode(namespace.encode()).decode()},
    }


def _kubeconfig(tmp_path):
    """A (non-empty) kubeconfig path; cluster_id probes it only when it exists."""
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\nkind: Config\n")
    return path


def test_cluster_id_reads_the_credentials_namespace(monkeypatch, tmp_path):
    payload = json.dumps({
        "items": [_secret("cattle-credentials-abc", "c-abc12")],
    })
    calls = []
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda args, **kw: calls.append(args) or _proc(0, payload),
    )
    assert rancher.cluster_id(_kubeconfig(tmp_path)) == "c-abc12"
    assert "--kubeconfig" in calls[0]
    # the namespace pre-check runs before the credentials secret is read
    assert calls[0][-3:] == ["get", "ns", "cattle-system"]
    assert calls[1][-6:] == ["get", "secret", "-n", "cattle-system", "-o", "json"]


def test_cluster_id_is_none_when_cattle_system_is_absent(monkeypatch, tmp_path):
    """A `NotFound` on the cattle-system namespace is a definitive "no agent"."""
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(1, stderr='Error from server (NotFound): namespaces '
                                   '"cattle-system" not found'),
    )
    assert rancher.cluster_id(_kubeconfig(tmp_path)) is None


def test_cluster_id_raises_when_cattle_system_cannot_be_checked(monkeypatch, tmp_path):
    """A namespace probe that fails for any other reason (api down, bad
    kubeconfig) must not read as "no agent": the reader cannot tell, so it
    raises instead of letting a caller re-register the cluster."""
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(1, stderr="The connection to the server was refused"),
    )
    with pytest.raises(ReconcileError, match="could not tell whether"):
        rancher.cluster_id(_kubeconfig(tmp_path))


def test_cluster_id_is_none_without_a_kubeconfig(tmp_path, monkeypatch):
    """No kubeconfig means the cluster was never converged: there is no
    downstream cluster to carry an agent, and nothing is shelled out to."""
    calls = []
    monkeypatch.setattr(
        rancher.kubectl, "_run", lambda args, **kw: calls.append(args)
    )
    assert rancher.cluster_id(tmp_path / "kubeconfig") is None
    assert calls == []


def test_cluster_id_reports_a_hung_kubectl_as_an_error(monkeypatch, tmp_path):
    """A timeout is not a "no agent" negative answer: the api accepted TCP but
    never answered, so the reader must raise a clear error instead of letting a
    raw TimeoutExpired leak or silently reporting the cluster as unregistered."""
    import subprocess

    def hung(*_a, **_k):
        raise subprocess.TimeoutExpired(["kubectl", "get", "ns"], 30)

    monkeypatch.setattr(rancher.kubectl, "_run", hung)
    with pytest.raises(ReconcileError, match="Rancher identity timed out"):
        rancher.cluster_id(_kubeconfig(tmp_path))


def test_cluster_id_raises_when_the_secret_read_fails(monkeypatch, tmp_path):
    """With cattle-system present, a failed credentials-secret read is a failed
    read, not an absent agent: the agent may well be registered, and reading
    None here would strip the Rancher annotation or re-register the cluster."""
    procs = [_proc(0), _proc(1, stderr="Error from server (Forbidden)")]
    monkeypatch.setattr(
        rancher.kubectl, "_run", lambda args, **kw: procs.pop(0),
    )
    with pytest.raises(ReconcileError, match="could not read the downstream"):
        rancher.cluster_id(_kubeconfig(tmp_path))


def test_cluster_id_is_none_without_a_credentials_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(0, json.dumps({"items": [
            _secret("some-other-secret", "c-abc12"),
            {"metadata": {"name": "cattle-credentials-x"}, "data": {}},
        ]})),
    )
    assert rancher.cluster_id(_kubeconfig(tmp_path)) is None


def test_cluster_id_raises_on_unparseable_output(monkeypatch, tmp_path):
    """Unparseable output while cattle-system exists is a failed read: reporting
    it as "no agent" would let converge re-register the cluster."""
    monkeypatch.setattr(
        rancher.kubectl, "_run", lambda *a, **k: _proc(0, "not json"),
    )
    with pytest.raises(ReconcileError, match="could not read the downstream"):
        rancher.cluster_id(_kubeconfig(tmp_path))


def test_cluster_id_is_none_on_unbase64able_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rancher.kubectl, "_run",
        lambda *a, **k: _proc(0, json.dumps({"items": [
            {"metadata": {"name": "cattle-credentials-zz"},
             "data": {"namespace": "not-base64!!"}},
        ]})),
    )
    assert rancher.cluster_id(_kubeconfig(tmp_path)) is None


def test_rancher_plugin_delegates_to_the_shared_helper(monkeypatch, tmp_path):
    """`downstream_rancher_id` must call the shared helper, so the rancher
    plugin no longer carries its own copy of the secret parsing."""
    from taloscluster_rancher import reconcile as _converge

    called = []
    monkeypatch.setattr(
        rancher, "cluster_id", lambda kc: called.append(kc) or "c-abc12",
    )
    assert _converge.downstream_rancher_id(tmp_path) == "c-abc12"
    assert called == [tmp_path / "kubeconfig"]


def test_rancher_plugin_surfaces_the_shared_helpers_no_agent_none(monkeypatch, tmp_path):
    """Absence ("no agent") is decided by the shared helper alone, so both
    plugins read the same distinction between an absent agent and a failed
    read that must raise instead."""
    from taloscluster_rancher import reconcile as _converge

    monkeypatch.setattr(rancher, "cluster_id", lambda kc: None)
    assert _converge.downstream_rancher_id(tmp_path) is None


def test_argocd_plugin_delegates_to_the_shared_helper(monkeypatch):
    """The argocd kube wrapper must call the shared helper too, so both plugins
    parse through one implementation."""
    from taloscluster_argocd import kube as argocd_kube

    called = []
    monkeypatch.setattr(rancher, "cluster_id", lambda kc: called.append(kc) or "c-abc12")
    assert argocd_kube.downstream_rancher_id(Path("/root")) == "c-abc12"
    assert called == [Path("/root") / "kubeconfig"]


def test_cluster_id_is_bounded(monkeypatch, tmp_path):
    """The shared reader routes through the kubectl wrapper's wall-clock bound, so
    a kube-api that accepts TCP but never answers cannot hang converge's check."""
    captured = {}

    def run(args, **kw):
        captured["timeout"] = kw.get("timeout")
        return _proc(0, json.dumps({"items": [_secret("cattle-credentials-a", "c-1")]}))

    monkeypatch.setattr(rancher.kubectl.subprocess, "run", run)
    assert rancher.cluster_id(_kubeconfig(tmp_path)) == "c-1"
    assert captured["timeout"] == rancher.kubectl.RUN_TIMEOUT


def test_argocd_kubectl_calls_are_bounded(monkeypatch, tmp_path):
    """argocd's apply/get/delete/diff all run through kubectl._run's wall-clock
    bound, so its converge/check hooks cannot hang on a non-responsive api. The
    read-only `get` probe keeps the short bound; apply/delete/diff get the longer
    manifest bound because a full apply or a server-side diff against a remote
    cluster can legitimately exceed it."""
    from taloscluster_argocd import kube as argocd_kube
    from taloscluster_argocd.config import ApplyTarget

    captured = []
    target = ApplyTarget(kubeconfig="kubeconfig", context=None)
    (tmp_path / "kubeconfig").write_text("clusters: []\n")

    def run(args, **kw):
        captured.append(kw.get("timeout"))
        return _proc(0)

    monkeypatch.setattr(argocd_kube.kubectl.subprocess, "run", run)
    monkeypatch.setattr(argocd_kube, "dry_run", lambda: False)
    monkeypatch.setattr(argocd_kube, "action", lambda _m: None)
    monkeypatch.setattr(argocd_kube, "info", lambda _l: None)

    argocd_kube.exists(target, tmp_path, "manifest")
    argocd_kube.exists_downstream(tmp_path, "manifest")
    argocd_kube.matches(target, tmp_path, "manifest")
    argocd_kube.apply(target, tmp_path, "manifest")
    argocd_kube.delete(target, tmp_path, "manifest")
    assert captured[:2] == [argocd_kube.kubectl.RUN_TIMEOUT] * 2
    assert captured[2:] == [argocd_kube.kubectl.MANIFEST_TIMEOUT] * 3
    assert argocd_kube.kubectl.MANIFEST_TIMEOUT > argocd_kube.kubectl.RUN_TIMEOUT


def test_argocd_wraps_the_reader_timeout_in_apply_error(monkeypatch):
    """The standalone argocd `downstream_rancher_id` must surface the shared
    reader's hung-kubectl error as an `ApplyError` (the type every other failure
    in kube.py uses), so a caller catches one error class for the whole plugin
    instead of having to also catch the core `ReconcileError` the reader raises.
    """
    from taloscluster_argocd import kube as argocd_kube
    from taloscluster_argocd.errors import ApplyError

    from taloscluster.errors import ReconcileError

    def _hung(_kc):
        raise ReconcileError("reading the downstream cluster's Rancher identity timed out")

    monkeypatch.setattr(rancher, "cluster_id", _hung)
    with pytest.raises(ApplyError) as exc:
        argocd_kube.downstream_rancher_id(Path("/root"))
    assert "Rancher identity timed out" in str(exc.value)


def test_rancher_kubectl_timeout_shows_the_trimmed_command(monkeypatch, tmp_path):
    """The rancher `_kubectl` timeout message names the full trimmed command
    (kubeconfig flag dropped, resource kept) rather than only the verb, so an
    operator sees which read hung instead of a bare `kubectl get`."""
    import subprocess

    from taloscluster_rancher import reconcile

    root = tmp_path
    (root / "kubeconfig").write_text("clusters: []\n")
    full_args = []

    def hung(args, **kw):
        full_args.append(args)
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(reconcile.kubectl, "_run", hung)
    with pytest.raises(reconcile.RancherError) as exc:
        reconcile._kubectl(root, "get", "ns", "cattle-system")
    message = str(exc.value)
    assert message.startswith("kubectl get ns cattle-system")
    assert "kubectl kubectl" not in message
    assert "--kubeconfig" not in message
    assert full_args[0][0] == "kubectl"
