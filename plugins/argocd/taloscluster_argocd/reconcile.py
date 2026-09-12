"""Reconcile this cluster's ArgoCD registration.

`converge` renders and applies (to the ArgoCD cluster, via kubectl) the manifests
that let ArgoCD manage this cluster:
  1. the cluster Secret (argocd.argoproj.io/secret-type: cluster),
  2. the AppProject with its admin/user roles,
  3. the repository Secret (argocd.argoproj.io/secret-type: repository),
  4. the root Application (app-of-apps), and
  5. the `<cluster>-cluster` Application with per-cluster (all-disabled by default)
     values.

When Cinder is enabled it also delivers the OpenStack cloud.conf Secret to this
cluster itself (its own kubeconfig, the `cinder-csi` namespace, whose Namespace
it ensures first), so the provider credential never flows through ArgoCD. When
cinder is disabled it removes any previously delivered Secret.

`destroy` removes them (apps, project, then secret, repo, cluster-apps).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from taloscluster.context import Context
from taloscluster.output import dry_run, info, log

from . import kube
from .config import ApplyTarget, Config, enabled
from .manifests import (
    CINDER_NAMESPACE,
    CINDER_SECRET_NAME,
    cinder_namespace,
    render,
)


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


def _deferred(ctx: Context) -> str | None:
    """Why argocd's converge is deferred during a `plan` before bootstrap.

    The cluster Secret and project destinations are built from this cluster's
    own kubeconfig, which converge only writes after bootstrap, and the rendered
    values consume the allocated kube-api / ingress endpoints. Before the first
    bootstrap neither exists, so there is nothing to register yet -- a `plan`
    must report the registration as deferred rather than fail with a confusing
    missing-kubeconfig error. Returns a human reason when the run is a dry-run
    and the cluster is not bootstrapped, None otherwise.
    """
    if not dry_run():
        return None
    if not ctx.kubeconfig.is_file():
        return "this cluster has no kubeconfig yet (it is written at bootstrap)"
    if not (ctx.kubernetes.get("floating_ip") or ctx.kubernetes.get("vip")):
        return "this cluster's kube-api endpoint is not allocated yet"
    return None


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

    reason = _deferred(ctx)
    if reason:
        info(f"argocd registration deferred ({reason}); nothing would be applied yet")
        return {"deferred": True, "reason": reason, "server": cfg.name}

    log("render manifests")
    m = render(cfg, ctx, git=_git(target), ost=_ost(target))

    if "cinder-secret" in m:
        log("apply cinder namespace and cloud-config secret to the cluster")
        kube.apply_downstream(ctx.root, cinder_namespace())
        kube.apply_downstream(ctx.root, m["cinder-secret"])
    elif cfg.openstack is not None and not enabled(cfg.cinder):
        log("remove orphaned cinder cloud-config secret (cinder disabled)")
        kube.delete_secret_downstream(ctx.root, CINDER_NAMESPACE, CINDER_SECRET_NAME)

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

    if "cinder-secret" in m:
        log("delete cinder cloud-config secret from the cluster")
        kube.delete_downstream(ctx.root, m["cinder-secret"])

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
    return {
        name: _probe(kube.exists, kube.exists_downstream, target, ctx.root, doc, name)
        for name, doc in m.items()
    }


def _matching(ctx: Context) -> dict[str, bool]:
    """Which rendered resources exactly match their live ArgoCD objects."""
    cfg, target = _load(ctx.root)
    _validate(target)
    m = render(cfg, ctx, git=_git(target), ost=_ost(target))
    return {
        name: _probe(kube.matches, kube.matches_downstream, target, ctx.root, doc, name)
        for name, doc in m.items()
    }


def _probe(
    argocd_fn: Callable[[ApplyTarget, Path, str], bool],
    downstream_fn: Callable[[Path, str], bool],
    target: ApplyTarget,
    root: Path,
    doc: str,
    name: str,
) -> bool:
    """Run a resource probe against ArgoCD or this cluster, whichever owns it."""
    if name == "cinder-secret":
        return downstream_fn(root, doc)
    return argocd_fn(target, root, doc)


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
    """Would converge apply anything? Not ok while a resource is missing or drifted."""
    matching = _matching(ctx)
    drifted = sorted(name for name, ok in matching.items() if not ok)
    return {"ok": not drifted, "drifted": drifted, "resources": matching}
