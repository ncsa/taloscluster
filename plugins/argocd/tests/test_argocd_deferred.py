"""ArgoCD converge reports its work deferred when a `plan` runs before bootstrap.

Before the first converge the cluster has no kubeconfig (it is written only
after bootstrap) and no allocated endpoints, so argocd's converge cannot render
or apply anything. It must report that clearly and stay a successful no-op
instead of failing the plan with a missing-kubeconfig error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from taloscluster import output
from taloscluster.context import Context

from taloscluster_argocd import reconcile
from taloscluster_argocd.config import ApplyTarget


def _ctx(tmp_path, kubeconfig=True, floating_ip=None, vip=None):
    if kubeconfig:
        (tmp_path / "kubeconfig").write_text(
            "apiVersion: v1\nkind: Config\nclusters: []\nusers: []\n"
        )
    return Context(
        root=tmp_path,
        cfg=None,
        status={
            "kubernetes": {
                "floating_ip": floating_ip or "",
                "vip": vip or "",
                "endpoint": f"https://{floating_ip}:6443" if floating_ip else "",
            },
            "ingress": {"floating_ip": "", "vip": "", "metallb": []},
        },
    )


def _stub_load(monkeypatch):
    monkeypatch.setattr(
        reconcile, "_load",
        lambda root: (SimpleNamespace(name="test", openstack=None, cinder={}),
                      ApplyTarget(context="argocd")),
    )
    monkeypatch.setattr(reconcile, "_validate", lambda _target: None)


@pytest.fixture(autouse=True)
def _reset_dry_run():
    output.set_dry_run(False)
    yield
    output.set_dry_run(False)


def test_plan_before_bootstrap_defers_and_does_not_render(tmp_path, monkeypatch, capsys):
    """No kubeconfig yet -> converge defers, never renders or applies."""
    ctx = _ctx(tmp_path, kubeconfig=False)
    _stub_load(monkeypatch)
    rendered = []

    monkeypatch.setattr(reconcile, "render", lambda *a, **k: rendered.append(True))

    output.set_dry_run(True)
    result = reconcile.converge(ctx)

    assert result["deferred"] is True
    assert "kubeconfig" in result["reason"]
    assert rendered == []
    out = capsys.readouterr().out
    assert "deferred" in out


def test_plan_with_kubeconfig_but_no_allocated_endpoint_defers(tmp_path, monkeypatch):
    """A kubeconfig alone is not enough: no kube-api endpoint yet -> deferred."""
    ctx = _ctx(tmp_path, kubeconfig=True, floating_ip=None, vip=None)
    _stub_load(monkeypatch)
    rendered = []

    monkeypatch.setattr(reconcile, "render", lambda *a, **k: rendered.append(True))

    output.set_dry_run(True)
    result = reconcile.converge(ctx)

    assert result["deferred"] is True
    assert "endpoint" in result["reason"]
    assert rendered == []


def test_plan_with_kubeconfig_and_endpoint_renders(tmp_path, monkeypatch):
    """Once the cluster is bootstrapped, a plan renders normally (not deferred)."""
    ctx = _ctx(tmp_path, kubeconfig=True, floating_ip="192.0.2.10", vip="192.0.2.10")
    _stub_load(monkeypatch)
    calls = []

    monkeypatch.setattr(reconcile, "render", lambda *a, **k: calls.append("render") or {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "apply", lambda _t, _r, doc: calls.append("apply"))

    output.set_dry_run(True)
    result = reconcile.converge(ctx)

    assert result.get("deferred") is None
    assert "render" in calls
    assert calls.count("apply") == 2


def test_real_converge_is_never_deferred(tmp_path, monkeypatch):
    """Deferral is a planning-only behaviour: a real converge always renders."""
    ctx = _ctx(tmp_path, kubeconfig=False)  # kubeconfig missing on purpose
    _stub_load(monkeypatch)

    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {"secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "apply", lambda _t, _r, doc: None)

    result = reconcile.converge(ctx)

    assert result.get("deferred") is None


def test_plan_with_configured_plugin_stays_successful(monkeypatch, tmp_path):
    """A deferred plugin keeps the plan exit code 0, so planning stays usable."""
    from taloscluster import plugins as core_plugins

    ctx = _ctx(tmp_path, kubeconfig=False)
    _stub_load(monkeypatch)

    mod = SimpleNamespace(
        AFTER=(),
        configured=lambda ctx: True,
        converge=lambda ctx, assume_yes=False: reconcile.converge(ctx),
    )
    ep = SimpleNamespace(name="argocd", load=lambda: mod)
    monkeypatch.setattr(core_plugins, "entry_points", lambda group: [ep])
    output.set_dry_run(True)
    try:
        rc = core_plugins.run(core_plugins.discover(), "converge", ctx, assume_yes=False)
    finally:
        output.set_dry_run(False)

    assert rc == 0
