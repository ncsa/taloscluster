"""ArgoCD check detects desired-content drift, not only missing objects."""

from __future__ import annotations

import subprocess
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

    monkeypatch.setattr(kube.kubectl, "_run", run)
    target = ApplyTarget(context="argocd")

    assert kube.matches(target, tmp_path, "kind: Secret\n") is matches
    assert seen["args"][0] == kube.kubectl.BIN
    assert seen["args"][-3:] == ["diff", "-f", "-"]
    assert seen["input"] == "kind: Secret\n"


def test_matches_reports_kubectl_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        kube.kubectl,
        "_run",
        lambda *a, **k: SimpleNamespace(returncode=2, stderr="forbidden"),
    )
    with pytest.raises(ApplyError, match="forbidden"):
        kube.matches(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")


def test_matches_turns_a_timeout_into_apply_error(monkeypatch, tmp_path):
    """A server-side diff that the api hangs on must be a clear error, not a raw
    TimeoutExpired -- and uses the longer manifest bound."""

    def hung(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))

    monkeypatch.setattr(kube.kubectl, "_run", hung)
    with pytest.raises(ApplyError, match="kubectl diff -f - timed out"):
        kube.matches(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")


def test_exists_turns_a_timeout_into_apply_error(monkeypatch, tmp_path):
    """The read-only `get` probe uses the short bound but must still surface a
    hung api as a clear error rather than a raw TimeoutExpired."""

    def hung(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))

    monkeypatch.setattr(kube.kubectl, "_run", hung)
    with pytest.raises(ApplyError, match="kubectl get -f - timed out"):
        kube.exists(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")


def test_apply_turns_a_timeout_into_apply_error(monkeypatch, tmp_path):
    monkeypatch.setattr(kube, "dry_run", lambda: False)
    monkeypatch.setattr(kube, "action", lambda _m: None)

    def hung(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))

    monkeypatch.setattr(kube.kubectl, "_run", hung)
    with pytest.raises(ApplyError, match="kubectl apply -f - timed out"):
        kube.apply(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")


def test_delete_turns_a_timeout_into_apply_error(monkeypatch, tmp_path):
    monkeypatch.setattr(kube, "dry_run", lambda: False)
    monkeypatch.setattr(kube, "action", lambda _m: None)

    def hung(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout", 30))

    monkeypatch.setattr(kube.kubectl, "_run", hung)
    with pytest.raises(ApplyError) as exc:
        kube.delete(ApplyTarget(context="argocd"), tmp_path, "kind: Secret\n")
    message = str(exc.value)
    assert message.startswith("kubectl delete -f - --ignore-not-found timed out")
    assert "kubectl kubectl" not in message


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


def test_converge_skips_the_cinder_secret_when_it_matches(monkeypatch):
    """A delivered cinder Secret that already matches is not applied again."""
    downstream = []
    cfg = SimpleNamespace(name="test")
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile.kube, "apply_downstream",
                        lambda root, doc: downstream.append(doc))
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "matches_downstream", lambda _r, _doc: True)
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p", "cinder-secret": "cr"})

    result = reconcile.converge(SimpleNamespace(root=None))

    assert downstream == []
    assert result["applied"] == []


def test_converge_applies_only_drifted_manifests(monkeypatch):
    """Converge compares each rendered manifest against the live object and
    applies only the missing or drifted ones, so a converged cluster plans no
    work instead of re-applying all five manifests on every run."""
    applied = []
    cfg = SimpleNamespace(name="test", openstack=None, cinder={})
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile.kube, "apply",
                        lambda _t, _r, doc: applied.append(doc))
    # every manifest matches except the cluster Secret and the repo credential
    monkeypatch.setattr(
        reconcile.kube, "matches",
        lambda _t, _r, doc: doc not in ("s", "r"),
    )
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p", "repo": "r", "apps": "a", "cluster-apps": "c"})

    result = reconcile.converge(SimpleNamespace(root=None))

    assert applied == ["s", "r"]
    assert result["applied"] == ["secret", "repo"]


def test_converge_deletes_orphaned_cinder_secret_when_disabled(monkeypatch):
    """Disabling cinder on an OpenStack cluster removes the delivered credential Secret."""
    cfg = SimpleNamespace(name="test", openstack=object(), cinder={"enabled": False})
    deleted = []
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile, "cinder_namespace", lambda: "ns")
    monkeypatch.setattr(reconcile.kube, "apply",
                        lambda _t, _r, doc: None)
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "secret_exists_downstream",
                        lambda root, ns, name: True)
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda root, ns, name: deleted.append((ns, name)))

    reconcile.converge(SimpleNamespace(root=None))
    assert deleted == [("cinder-csi", "cinder-csi-cloud-config")]


def test_converge_leaves_an_absent_orphaned_cinder_secret_alone(monkeypatch):
    """No delivered Secret, nothing to clean up: the delete (and its plan line)
    is skipped instead of issuing a doomed `--ignore-not-found` every run."""
    cfg = SimpleNamespace(name="test", openstack=object(), cinder={"enabled": False})
    deleted = []
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "secret_exists_downstream",
                        lambda root, ns, name: False)
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda root, ns, name: deleted.append((ns, name)))

    reconcile.converge(SimpleNamespace(root=None))
    assert deleted == []


def test_converge_does_not_cleanup_orphan_on_non_openstack(monkeypatch):
    """Clusters without OpenStack never issue the cinder cleanup, however cinder is set."""
    cfg = SimpleNamespace(name="test", openstack=None, cinder={"enabled": False})
    deleted = []
    monkeypatch.setattr(
        reconcile, "_load", lambda root: (cfg, ApplyTarget(context="argocd"))
    )
    monkeypatch.setattr(reconcile, "render", lambda *a, **k: {
        "secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "apply",
                        lambda _t, _r, doc: None)
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _doc: True)
    monkeypatch.setattr(reconcile.kube, "secret_exists_downstream",
                        lambda root, ns, name: pytest.fail("must not probe"))
    monkeypatch.setattr(reconcile.kube, "delete_secret_downstream",
                        lambda root, ns, name: deleted.append((ns, name)))

    reconcile.converge(SimpleNamespace(root=None))
    assert deleted == []


def test_destroy_deletes_cinder_secret_from_cluster(monkeypatch):
    deleted = []
    named = []
    monkeypatch.setattr(reconcile, "_load", lambda root: (object(), ApplyTarget(context="argocd")))

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


# ---- Rancher cluster identity ----------------------------------------------

def test_standalone_converge_resolves_the_downstream_rancher_id(monkeypatch):
    """`plugin argocd converge` runs without rancher, so argocd reads the same
    cluster id off the downstream agent itself and stamps it -- a standalone
    run must not re-write the cluster Secret with a blank Rancher annotation."""
    ctx = SimpleNamespace(root="/some/root", results={})
    monkeypatch.setattr(kube, "downstream_rancher_id", lambda root: "c-abc12")
    monkeypatch.setattr(reconcile, "_load",
                        lambda root: (SimpleNamespace(name="t", openstack=None),
                                      ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "render",
                        lambda *a, **k: {"secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _d: False)
    monkeypatch.setattr(reconcile.kube, "apply", lambda _t, _r, _d: None)

    assert reconcile.converge(ctx)["applied"] == ["secret", "project"]
    assert ctx.results["rancher"]["cluster_id"] == "c-abc12"


def test_standalone_check_resolves_the_downstream_rancher_id(monkeypatch):
    """The standalone `plugin argocd check` renders with the same id before
    diffing, so the just-applied annotated Secret reads as current, not drifted."""
    ctx = SimpleNamespace(root="/some/root", results={})
    monkeypatch.setattr(kube, "downstream_rancher_id", lambda root: "c-abc12")
    monkeypatch.setattr(reconcile, "_load",
                        lambda root: (object(), ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "render",
                        lambda *a, **k: {"secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _d: True)

    assert reconcile.check(ctx) == {
        "ok": True, "drifted": [], "resources": {"secret": True, "project": True}
    }
    assert ctx.results["rancher"]["cluster_id"] == "c-abc12"


def test_no_rancher_id_when_the_agent_is_absent(monkeypatch):
    """An unregistered cluster (no cattle agent) is normal, not an error -- the
    standalone run renders without the Rancher annotation, mirroring rancher
    not being installed."""
    ctx = SimpleNamespace(root="/some/root", results={})
    monkeypatch.setattr(kube, "downstream_rancher_id", lambda root: None)
    monkeypatch.setattr(reconcile, "_load",
                        lambda root: (SimpleNamespace(name="t", openstack=None),
                                      ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "render",
                        lambda *a, **k: {"secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _d: False)
    monkeypatch.setattr(reconcile.kube, "apply", lambda _t, _r, _d: None)

    reconcile.converge(ctx)
    assert "rancher" not in ctx.results


def test_rancher_report_wins_without_a_kubectl_call(monkeypatch):
    """When rancher ran first and put the id in ctx.results, argocd must reuse
    it (authoritative and cheap) instead of shelling out to read it again."""
    ctx = SimpleNamespace(root="/some/root", results={"rancher": {"cluster_id": "c-abc12"}})
    called = []
    monkeypatch.setattr(kube, "downstream_rancher_id", lambda root: called.append(root))
    monkeypatch.setattr(reconcile, "_load",
                        lambda root: (object(), ApplyTarget(context="argocd")))
    monkeypatch.setattr(reconcile, "render",
                        lambda *a, **k: {"secret": "s", "project": "p"})
    monkeypatch.setattr(reconcile.kube, "matches", lambda _t, _r, _d: True)

    assert reconcile.check(ctx)["ok"] is True
    assert called == []
