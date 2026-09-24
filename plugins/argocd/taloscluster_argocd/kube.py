"""kubectl helpers to apply manifests to the ArgoCD cluster.

Uses the kubeconfig (and optional `--context`) configured under `argocd:` in
secrets.yaml. The kubeconfig path is resolved against the cluster directory.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

import yaml
from taloscluster.errors import ReconcileError
from taloscluster.k8s import kubectl, rancher
from taloscluster.output import action, dry_run, info, redact

from .config import ApplyTarget
from .errors import ApplyError


def _timed_out(command: str) -> str:
    """Message for a kubectl command that hung on the api instead of answering."""
    return (
        f"{command} timed out; the api accepted TCP but never answered. "
        "Investigate the cluster and retry"
    )


def _base_args(target: ApplyTarget, root: Path) -> list[str]:
    args = [kubectl.BIN]
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
    return [kubectl.BIN, "--kubeconfig", str((root / "kubeconfig").resolve())]


def exists(target: ApplyTarget, root: Path, manifest: str) -> bool:
    """Whether every resource in a manifest is already present on the cluster.

    `kubectl get -f -` succeeds only when it finds all of them, which is exactly
    the question `check` asks. Read-only, so it runs under --dry-run too.
    """
    return _run_get(_base_args(target, root), manifest)


def exists_downstream(root: Path, manifest: str) -> bool:
    """Like :func:`exists`, but against this cluster via its own kubeconfig."""
    return _run_get(_downstream_args(root), manifest)


def secret_exists_downstream(root: Path, namespace: str, name: str) -> bool:
    """Whether one named Secret exists on this cluster (its own kubeconfig).

    Read-only, so it runs under --dry-run too. Companion to
    :func:`delete_secret_downstream`, which removes that Secret: the existence
    probe lets converge skip the delete (and the plan line for it) when there is
    nothing to remove.
    """
    args = _downstream_args(root) + [
        "get", "secret", name, "--namespace", namespace,
    ]
    try:
        proc = kubectl._run(args, capture=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out(kubectl.display(args))) from e
    return proc.returncode == 0


def downstream_rancher_id(root: Path) -> str | None:
    """This cluster's own Rancher cluster id (c-xxxxx), or None when the agent is absent.

    Read from the cattle-cluster-agent's ``cattle-credentials-*`` secret in
    cattle-system, the same value the rancher plugin's `downstream_rancher_id`
    returns (via the shared `taloscluster.k8s.rancher` helper). The standalone
    ``plugin argocd converge|check`` runs argocd without rancher, so argocd
    resolves the id itself rather than rendering the Rancher annotation empty
    and re-writing the cluster Secret without it (or reporting drift against the
    just-applied one).
    """
    try:
        return rancher.cluster_id(root / "kubeconfig")
    except ReconcileError as e:
        raise ApplyError(str(e)) from e


def _run_get(base: list[str], manifest: str) -> bool:
    args = base + ["get", "-f", "-"]
    try:
        proc = kubectl._run(args, input=manifest, capture=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out("kubectl get -f -")) from e
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
    try:
        proc = kubectl._run(
            args, input=manifest, capture=True, check=False, timeout=kubectl.MANIFEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out("kubectl diff -f -")) from e
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
        _show_diff(base, manifest)
        return
    action(message)
    try:
        proc = kubectl._run(
            args, input=manifest, capture=True, check=False, timeout=kubectl.MANIFEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out("kubectl apply -f -")) from e
    if proc.returncode != 0:
        raise ApplyError(f"kubectl apply failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)


#: metadata the server rewrites on every write; noise in a preview
_SERVER_METADATA = ("managedFields", "resourceVersion", "uid", "creationTimestamp", "generation")
#: holds a clear-text copy of the whole applied manifest, Secret values included
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _show_diff(base: list[str], manifest: str) -> None:
    """Print what applying `manifest` would change, with credentials redacted.

    `kubectl diff` output is not shown as is: it prints the last-applied
    annotation, a clear-text copy of a Secret's values. Instead the live object
    and the one the server would store (a server-side dry-run apply) are each
    passed through `redact` and diffed here. An Application's Helm values are
    one YAML string, so they are parsed first: `redact` then sees the keys
    inside them, and the diff shows the changed values line by line. A Secret
    whose only change is in its (redacted) values gets a one-line note instead
    of a diff.
    """
    for doc in yaml.safe_load_all(manifest):
        if not doc:
            continue
        text = yaml.safe_dump(doc)
        live = _fetch(base + ["get", "-f", "-", "-o", "yaml"], text)
        desired = _fetch(base + ["apply", "--dry-run=server", "-f", "-", "-o", "yaml"], text)
        before, after = _strip(live), _strip(desired or doc)
        ref = f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"
        diff = list(difflib.unified_diff(
            _dump(redact(before)), _dump(redact(after)),
            f"live {ref}", f"desired {ref}", lineterm="",
        ))
        for line in diff:
            info("      " + line)
        if not diff and before != after:
            info(f"      {ref}: secret values differ (not shown)")


def _fetch(args: list[str], text: str) -> dict | None:
    """The object a read-only kubectl call prints as yaml, or None if it failed."""
    try:
        proc = kubectl._run(
            args, input=text, capture=True, check=False, timeout=kubectl.MANIFEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out(kubectl.display(args))) from e
    if proc.returncode != 0:
        return None
    return yaml.safe_load(proc.stdout)


def _strip(obj: dict | None) -> dict | None:
    """Copy of `obj` without status and the server-managed metadata, its Helm
    values parsed. A one-item `kind: List` (what `kubectl get -f -` prints) is
    unwrapped to that item."""
    if obj is None:
        return None
    if obj.get("kind") == "List" and len(obj.get("items") or []) == 1:
        obj = obj["items"][0]
    obj = {k: _parse_values(v) for k, v in obj.items() if k != "status"}
    meta = {k: v for k, v in (obj.get("metadata") or {}).items() if k not in _SERVER_METADATA}
    annotations = {k: v for k, v in (meta.get("annotations") or {}).items() if k != _LAST_APPLIED}
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    obj["metadata"] = meta
    return obj


def _parse_values(node: Any) -> Any:
    """`node` with every `helm.values` string replaced by the mapping it holds.

    A values string that is not a YAML mapping is masked whole, since `redact`
    could not look inside it.
    """
    if isinstance(node, list):
        return [_parse_values(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _parse_values(v) for k, v in node.items()}
    helm = out.get("helm")
    if isinstance(helm, dict) and isinstance(helm.get("values"), str):
        try:
            values = yaml.safe_load(helm["values"])
        except yaml.YAMLError:
            values = "REDACTED"
        if values is not None and not isinstance(values, dict):
            values = "REDACTED"
        out["helm"] = {**helm, "values": values}
    return out


def _dump(obj: dict | None) -> list[str]:
    return yaml.safe_dump(obj, default_flow_style=False).splitlines() if obj else []


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
    try:
        proc = kubectl._run(
            args, input=manifest, capture=True, check=False, timeout=kubectl.MANIFEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise ApplyError(_timed_out(kubectl.display(args))) from e
    if proc.returncode != 0:
        raise ApplyError(f"kubectl delete failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)
