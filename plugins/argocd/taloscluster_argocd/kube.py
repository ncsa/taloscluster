"""kubectl helpers to apply manifests to the ArgoCD cluster.

Uses the kubeconfig (and optional `--context`) configured under `argocd:` in
secrets.yaml. The kubeconfig path is resolved against the cluster directory.
"""

from __future__ import annotations

import base64
import json
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


def _downstream_args(root: Path) -> list[str]:
    """kubectl base args for this cluster itself (its own kubeconfig).

    Used to deliver out-of-band resources the ArgoCD-triggered charts consume on
    this cluster (for example the Cinder cloud.conf Secret in the ``cinder-csi``
    namespace), so a provider credential never has to pass through ArgoCD.
    """
    return ["kubectl", "--kubeconfig", str((root / "kubeconfig").resolve())]


def exists(target: ApplyTarget, root: Path, manifest: str) -> bool:
    """Whether every resource in a manifest is already present on the cluster.

    `kubectl get -f -` succeeds only when it finds all of them, which is exactly
    the question `check` asks. Read-only, so it runs under --dry-run too.
    """
    return _run_get(_base_args(target, root), manifest)


def exists_downstream(root: Path, manifest: str) -> bool:
    """Like :func:`exists`, but against this cluster via its own kubeconfig."""
    return _run_get(_downstream_args(root), manifest)


def downstream_rancher_id(root: Path) -> str | None:
    """This cluster's own Rancher cluster id (c-xxxxx), or None when the agent is absent.

    Read from the cattle-cluster-agent's ``cattle-credentials-*`` secret in
    cattle-system, the same value the rancher plugin's `downstream_rancher_id`
    returns. The standalone ``plugin argocd converge|check`` runs argocd without
    rancher, so argocd resolves the id itself rather than rendering the Rancher
    annotation empty and re-writing the cluster Secret without it (or reporting
    drift against the just-applied one).
    """
    proc = subprocess.run(
        _downstream_args(root)
        + ["get", "secret", "-n", "cattle-system", "-o", "json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return None
    for item in doc.get("items", []):
        if item.get("metadata", {}).get("name", "").startswith("cattle-credentials"):
            ns = item.get("data", {}).get("namespace")
            if ns:
                try:
                    return base64.b64decode(ns).decode()
                except Exception:
                    pass
    return None


def _run_get(base: list[str], manifest: str) -> bool:
    args = base + ["get", "-f", "-"]
    proc = subprocess.run(args, input=manifest, text=True, capture_output=True)
    return proc.returncode == 0


def matches(target: ApplyTarget, root: Path, manifest: str) -> bool:
    """Whether the live resource content matches this desired manifest.

    `kubectl diff` performs the same server-side normalization used by apply:
    exit 0 means equal, 1 means drift, and larger values are actual errors.
    """
    return _run_diff(_base_args(target, root), manifest)


def matches_downstream(root: Path, manifest: str) -> bool:
    """Like :func:`matches`, but against this cluster via its own kubeconfig."""
    return _run_diff(_downstream_args(root), manifest)


def _run_diff(base: list[str], manifest: str) -> bool:
    args = base + ["diff", "-f", "-"]
    proc = subprocess.run(args, input=manifest, text=True, capture_output=True)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ApplyError(f"kubectl diff failed: {proc.stderr.strip()}")


def apply(target: ApplyTarget, root: Path, manifest: str) -> None:
    """Apply a manifest to the ArgoCD cluster (kubectl apply -f -)."""
    _run_apply(
        _base_args(target, root),
        manifest,
        "applying manifest to ArgoCD cluster via kubectl",
        "ARGOCD_MANIFEST",
    )


def apply_downstream(root: Path, manifest: str) -> None:
    """Apply a manifest to this cluster (its own kubeconfig)."""
    _run_apply(
        _downstream_args(root),
        manifest,
        "applying manifest to the cluster via kubectl",
        "DOWNSTREAM_MANIFEST",
    )


def _run_apply(base: list[str], manifest: str, message: str, label: str) -> None:
    args = base + ["apply", "-f", "-"]
    if dry_run():
        action(f"kubectl apply {label} " + " ".join(args[1:]))
        return
    action(message)
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
    _delete(
        args,
        manifest,
        "deleting manifest from ArgoCD cluster via kubectl",
        "ARGOCD_MANIFEST",
    )


def delete_downstream(root: Path, manifest: str) -> None:
    """Delete the resources described by a manifest from this cluster."""
    args = _downstream_args(root) + ["delete", "-f", "-", "--ignore-not-found"]
    _delete(
        args,
        manifest,
        "deleting manifest from the cluster via kubectl",
        "DOWNSTREAM_MANIFEST",
    )


def delete_secret_downstream(root: Path, namespace: str, name: str) -> None:
    """Delete one named Secret from this cluster (used to clear an orphaned cinder secret).

    Independent of any rendered manifest so it works even when cinder is disabled
    (and thus the Secret manifest is not rendered). ``--ignore-not-found`` makes
    it a no-op when nothing is there to delete.
    """
    args = _downstream_args(root) + [
        "delete",
        "secret",
        name,
        "--namespace",
        namespace,
        "--ignore-not-found",
    ]
    _delete(
        args,
        "",
        "deleting orphaned cinder secret from the cluster via kubectl",
        "DOWNSTREAM_MANIFEST",
    )


def _delete(args: list[str], manifest: str, message: str, label: str) -> None:
    if dry_run():
        action(f"kubectl delete {label} " + " ".join(args[1:]))
        return
    action(message)
    proc = subprocess.run(args, input=manifest, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ApplyError(f"kubectl delete failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)
