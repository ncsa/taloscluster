"""kubectl helpers for namespaces, manifests, and the metallb pool resources.

`target` is either "-" (a manifest fed on stdin) or a url -- kubectl treats
`-f <url>` exactly like `-f -`. Everything runs against this cluster's own
kubeconfig, like argocd's downstream helpers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from taloscluster.errors import ReconcileError
from taloscluster.k8s import kubectl
from taloscluster.output import action, dry_run, info


def _args(root: Path) -> list[str]:
    return [kubectl.BIN, "--kubeconfig", str((root / "kubeconfig").resolve())]


def _run(
    root: Path, args: list[str], *, input: str | None = None, timeout: float = kubectl.RUN_TIMEOUT
) -> subprocess.CompletedProcess:
    return kubectl._run(_args(root) + args, capture=True, check=False, timeout=timeout, input=input)


def exists(root: Path, target: str, *, input: str | None = None) -> bool:
    """Every resource in `target` is present (kubectl get -f). Read-only."""
    return _run(root, ["get", "-f", target], input=input).returncode == 0


def namespace_labels(root: Path, name: str) -> dict[str, str] | None:
    """The live namespace's labels, or None when it cannot be read.

    Read-only. A failed get -- an absent namespace, an api that will not
    answer -- reads as None, so a caller deciding whether a namespace is the
    plugin's own never deletes on a guess.
    """
    proc = _run(root, ["get", "namespace", name, "-o", "json"])
    if proc.returncode != 0:
        return None
    try:
        metadata = json.loads(proc.stdout).get("metadata") or {}
    except ValueError:
        return None
    return metadata.get("labels") or {}


def wait_deployment_available(
    root: Path, name: str, namespace: str, timeout: str = "180s"
) -> bool:
    """Wait until a deployment reports available; best effort, for webhook warmup."""
    proc = _run(
        root,
        ["wait", "--for=condition=available", f"deployment/{name}",
         "--namespace", namespace, f"--timeout={timeout}"],
        timeout=200.0,
    )
    return proc.returncode == 0


def matches(root: Path, target: str, *, input: str | None = None) -> bool:
    """Live content equals the desired manifest (kubectl diff -f).

    Exit 0 means equal, 1 means drifted (or absent -- `kubectl diff` renders a
    missing resource as an add), larger values are actual errors.
    """
    proc = _run(root, ["diff", "-f", target], input=input, timeout=kubectl.MANIFEST_TIMEOUT)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ReconcileError(f"kubectl diff failed: {proc.stderr.strip()}")


def apply(root: Path, target: str, *, label: str = "", input: str | None = None) -> None:
    args = ["apply", "-f", target]
    if dry_run():
        action(f"kubectl apply {label or target}".strip())
        return
    action(f"applying {label or 'manifest'} via kubectl")
    proc = _run(root, args, input=input, timeout=kubectl.MANIFEST_TIMEOUT)
    if proc.returncode != 0:
        raise ReconcileError(f"kubectl apply failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)


def delete(root: Path, target: str, *, label: str = "", input: str | None = None) -> None:
    args = ["delete", "-f", target, "--ignore-not-found"]
    if dry_run():
        action(f"kubectl delete {label or target}".strip())
        return
    action(f"deleting {label or 'manifest'} via kubectl")
    proc = _run(root, args, input=input, timeout=kubectl.MANIFEST_TIMEOUT)
    if proc.returncode != 0:
        raise ReconcileError(f"kubectl delete failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)
