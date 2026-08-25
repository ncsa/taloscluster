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
