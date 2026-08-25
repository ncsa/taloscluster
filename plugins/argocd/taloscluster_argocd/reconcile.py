"""Reconcile this cluster's ArgoCD registration.

`converge` renders and applies (to the ArgoCD cluster, via kubectl) the manifests
that let ArgoCD manage this cluster:
  1. the cluster Secret (argocd.argoproj.io/secret-type: cluster),
  2. the AppProject with its admin/user roles,
  3. the repository Secret (argocd.argoproj.io/secret-type: repository),
  4. the root Application (app-of-apps), and
  5. the `<cluster>-cluster` Application with per-cluster (all-disabled by default)
     values.

`destroy` removes them (apps, project, then secret, repo, cluster-apps).
"""

from __future__ import annotations

from pathlib import Path

from taloscluster.context import Context
from taloscluster.output import info, log

from . import kube
from .config import ApplyTarget, Config
from .manifests import render


def _load(root: Path):
    cfg = Config.load(root)
    target = Config.load_secrets(root)
    return cfg, target


def _validate(target: ApplyTarget) -> None:
    if not target.uses_kubectl:
        raise RuntimeError(
            "argocd uses url+token mode, but kubectl mode is required; set "
            "argocd.kubeconfig or argocd.context in secrets.yaml"
        )


def _git(target: ApplyTarget) -> tuple[str, str] | None:
    if target.git_username is None and target.git_token is None:
        return None
    return (target.git_username or "", target.git_token or "")


def _ost(target: ApplyTarget) -> tuple[str, str] | None:
    if target.openstack_credential_id is None and target.openstack_credential_secret is None:
        return None
    return (target.openstack_credential_id or "", target.openstack_credential_secret or "")


def converge(ctx: Context, assume_yes: bool = False) -> dict:
    cfg, target = _load(ctx.root)
    _validate(target)

    log("render manifests")
    m = render(cfg, ctx, git=_git(target), ost=_ost(target))

    log("apply cluster secret to ArgoCD")
    kube.apply(target, ctx.root, m["secret"])

    log("apply app project to ArgoCD")
    kube.apply(target, ctx.root, m["project"])

    if "repo" in m:
        log("apply git repository secret to ArgoCD")
        kube.apply(target, ctx.root, m["repo"])

    if "apps" in m:
        log("apply root application to ArgoCD")
        kube.apply(target, ctx.root, m["apps"])

    if "cluster-apps" in m:
        log("apply cluster apps application to ArgoCD")
        kube.apply(target, ctx.root, m["cluster-apps"])
    info("done")
    return {"applied": sorted(m), "server": cfg.name}


def destroy(ctx: Context, assume_yes: bool = False) -> None:
    cfg, target = _load(ctx.root)
    _validate(target)

    log("render manifests")
    m = render(cfg, ctx, git=_git(target), ost=_ost(target))

    if "apps" in m:
        log("delete root application from ArgoCD")
        kube.delete(target, ctx.root, m["apps"])

    if "cluster-apps" in m:
        log("delete cluster apps application from ArgoCD")
        kube.delete(target, ctx.root, m["cluster-apps"])

    log("delete app project from ArgoCD")
    kube.delete(target, ctx.root, m["project"])

    log("delete cluster secret from ArgoCD")
    kube.delete(target, ctx.root, m["secret"])

    if "repo" in m:
        log("delete git repository secret from ArgoCD")
        kube.delete(target, ctx.root, m["repo"])
    info("done")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _present(ctx: Context) -> dict[str, bool]:
    """Which of the rendered manifests already exist on the ArgoCD cluster."""
    cfg, target = _load(ctx.root)
    _validate(target)
    m = render(cfg, ctx, git=_git(target), ost=_ost(target))
    return {name: kube.exists(target, ctx.root, doc) for name, doc in m.items()}


def status(ctx: Context) -> dict:
    """Which pieces of this cluster's ArgoCD registration are in place."""
    cfg, target = _load(ctx.root)
    return {
        "cluster": cfg.name,
        "context": target.context or "",
        "kubeconfig": target.kubeconfig or "",
        "resources": _present(ctx),
    }


def check(ctx: Context) -> dict:
    """Would a converge apply anything? Not ok while a manifest is missing."""
    present = _present(ctx)
    missing = sorted(name for name, ok in present.items() if not ok)
    return {"ok": not missing, "missing": missing, "resources": present}
