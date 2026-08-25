"""kubectl helpers to apply manifests to the ArgoCD cluster.

Uses the kubeconfig (and optional `--context`) configured under `argocd:` in
secrets.yaml. The kubeconfig path is resolved against the cluster directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from taloscluster.output import action, dry_run, info

from .config import ApplyTarget
from .errors import ApplyError


def _base_args(target: ApplyTarget, root: Path) -> list[str]:
    args = ["kubectl"]
    if target.kubeconfig:
        kc = (root / target.kubeconfig).resolve()
        args += ["--kubeconfig", str(kc)]
    if target.context:
        args += ["--context", target.context]
    return args


def exists(target: ApplyTarget, root: Path, manifest: str) -> bool:
    """Whether every resource in a manifest is already present on the cluster.

    `kubectl get -f -` succeeds only when it finds all of them, which is exactly
    the question `check` asks. Read-only, so it runs under --dry-run too.
    """
    args = _base_args(target, root) + ["get", "-f", "-"]
    proc = subprocess.run(args, input=manifest, text=True, capture_output=True)
    return proc.returncode == 0


def matches(target: ApplyTarget, root: Path, manifest: str) -> bool:
    """Whether the live resource content matches this desired manifest.

    `kubectl diff` performs the same server-side normalization used by apply:
    exit 0 means equal, 1 means drift, and larger values are actual errors.
    """
    args = _base_args(target, root) + ["diff", "-f", "-"]
    proc = subprocess.run(args, input=manifest, text=True, capture_output=True)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ApplyError(f"kubectl diff failed: {proc.stderr.strip()}")


def apply(target: ApplyTarget, root: Path, manifest: str) -> None:
    """Apply a manifest to the ArgoCD cluster (kubectl apply -f -)."""
    args = _base_args(target, root) + ["apply", "-f", "-"]
    if dry_run():
        action("kubectl apply ARGOCD_MANIFEST " + " ".join(args[1:]))
        return
    action("applying manifest to ArgoCD cluster via kubectl")
    proc = subprocess.run(
        args, input=manifest, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise ApplyError(f"kubectl apply failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)


def delete(target: ApplyTarget, root: Path, manifest: str) -> None:
    """Delete the resources described by a manifest from the ArgoCD cluster."""
    args = _base_args(target, root) + ["delete", "-f", "-", "--ignore-not-found"]
    if dry_run():
        action("kubectl delete ARGOCD_MANIFEST " + " ".join(args[1:]))
        return
    action("deleting manifest from ArgoCD cluster via kubectl")
    proc = subprocess.run(
        args, input=manifest, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise ApplyError(f"kubectl delete failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)
