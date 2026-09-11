"""ArgoCD check detects desired-content drift, not only missing objects."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taloscluster_argocd import kube, reconcile
from taloscluster_argocd.config import ApplyTarget
from taloscluster_argocd.errors import ApplyError


@pytest.mark.parametrize(("returncode", "matches"), [(0, True), (1, False)])
def test_matches_uses_kubectl_diff(monkeypatch, tmp_path, returncode, matches):
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs["input"]
        return SimpleNamespace(returncode=returncode, stderr="")

    monkeypatch.setattr(kube.subprocess, "run", run)
    target = ApplyTarget(context="argocd")

    assert kube.matches(target, tmp_path, "kind: Secret\n") is matches
    assert seen["args"][-3:] == ["diff", "-f", "-"]
    assert seen["input"] == "kind: Secret\n"


def test_matches_reports_kubectl_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        kube.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stderr="forbidden"),
    )
    with pytest.raises(ApplyError, match="forbidden"):
        kube.matches(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")


def test_check_reports_drifted_resources(monkeypatch):
    ctx = SimpleNamespace(root=None)
    target = ApplyTarget(context="argocd")
    monkeypatch.setattr(reconcile, "_load", lambda root: (object(), target))
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {"secret": "a", "project": "b"})
    monkeypatch.setattr(reconcile, "_git", lambda _target: None)
    monkeypatch.setattr(reconcile, "_ost", lambda _target: None)
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, doc: doc == "b")

    assert reconcile.check(ctx) == {
        "ok": False,
        "drifted": ["secret"],
        "resources": {"secret": False, "project": True},
    }


def test_converge_applies_cinder_secret_to_cluster_not_argocd(monkeypatch):
    """The provider credential reaches the downstream cluster, never ArgoCD."""
    downstream = []
    argocd = []
    cfg = SimpleNamespace(name="test")
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "_validate", lambda _target: None)

    def mk(name, fn):
        monkeypatch.setattr(reconcile.kube, name, fn)

    mk("apply_downstream", lambda root, doc: downstream.append(doc))
    mk("apply", lambda _t, _r, doc: argocd.append(doc))
    for name in ("delete_downstream", "delete", "delete_secret_downstream",
                 "exists_downstream", "matches", "exists", "matches_downstream"):
        monkeypatch.setattr(reconcile.kube, name, lambda *a, **k: None)

    def fake_render(*a, **k):
        return {"secret": "s", "project": "p", "cluster-apps": "c", "cinder-secret": "cr"}

    monkeypatch.setattr(reconcile, "render", fake_render)
    reconcile.converge(SimpleNamespace(root=None))

    assert "cr" in downstream
    assert "cr" not in argocd


def test_converge_deletes_orphaned_cinder_secret_when_disabled(monkeypatch):
    """Disabling cinder on an OpenStack cluster removes the delivered credential Secret."""
    cfg = SimpleNamespace(name="test", openstack=object(), cinder={"enabled": False})
    deleted = []
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "_validate", lambda _target: None)
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile, "cinder_namespace", lambda: "ns")
    monkeypatch.setattr(reconcile.kube, "apply",
                        lambda _t, _r, doc: None)
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda root, ns, name: deleted.append((ns, name)))

    reconcile.converge(SimpleNamespace(root=None))
    assert deleted == [("cinder-csi", "cinder-csi-cloud-config")]


def test_converge_does_not_cleanup_orphan_on_non_openstack(monkeypatch):
    """Clusters without OpenStack never issue the cinder cleanup, however cinder is set."""
    cfg = SimpleNamespace(name="test", openstack=None, cinder={"enabled": False})
    deleted = []
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "_validate", lambda _target: None)
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "apply",
                        lambda _t, _r, doc: None)
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda root, ns, name: deleted.append((ns, name)))

    reconcile.converge(SimpleNamespace(root=None))
    assert deleted == []


def test_destroy_deletes_cinder_secret_from_cluster(monkeypatch):
    deleted = []
    named = []
    monkeypatch.setattr(reconcile, "_load", lambda root: (object(), ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "_validate", lambda _target: None)

    def mk(name, fn):
        monkeypatch.setattr(reconcile.kube, name, fn)

    mk("delete_downstream", lambda root, doc: deleted.append(doc))
    monkeypatch.setattr(reconcile.kube, "delete", lambda _t, _r, _doc: None)
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda *a, **k: named.append(a))
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p", "repo": "r", "apps": "a",
        "cluster-apps": "c", "cinder-secret": "cr"})
    reconcile.destroy(SimpleNamespace(root=None))
    assert deleted == ["cr"]
    # destroy removes just the Secret (the Secret-only manifest), never the namespace
    assert named == []


def test_check_probes_cinder_secret_downstream(monkeypatch):
    """check must compare the delivered Secret against this cluster, not ArgoCD."""
    ctx = SimpleNamespace(root=None)
    monkeypatch.setattr(reconcile, "_load", lambda root: (object(), ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {"secret": "a", "cinder-secret": "cr"})
    monkeypatch.setattr(reconcile, "_git", lambda _target: None)
    monkeypatch.setattr(reconcile, "_ost", lambda _target: ("id", "secret"))
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "matches_downstream", lambda _r, doc: doc == "cr")

    assert reconcile.check(ctx) == {
        "ok": True,
        "drifted": [],
        "resources": {"secret": True, "cinder-secret": True},
    }


def test_present_probes_cinder_secret_downstream(monkeypatch):
    """status (_present) also compares the delivered Secret against this cluster."""
    ctx = SimpleNamespace(root=None)
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (object(), ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {"secret": "a", "cinder-secret": "cr"})
    monkeypatch.setattr(reconcile, "_git", lambda _target: None)
    monkeypatch.setattr(reconcile, "_ost", lambda _target: ("id", "secret"))
    monkeypatch.setattr(reconcile.kube, "exists", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "exists_downstream", lambda _r, doc: doc == "cr")

    assert reconcile._present(ctx) == {"secret": True, "cinder-secret": True}
