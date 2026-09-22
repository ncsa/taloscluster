"""Thin subprocess wrapper around helm.

Mirrors taloscluster.k8s.kubectl: bounded subprocess runs, JSON parsed not
scraped. Every cluster-touching command targets this cluster via --kubeconfig.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml
from taloscluster.errors import ReconcileError
from taloscluster.output import action, dry_run, info

BIN = "helm"
# a chart install/upgrade pulling images can legitimately take minutes
UPGRADE_TIMEOUT = 600.0
# helm list is a local api call; show chart downloads chart metadata from the repo
QUERY_TIMEOUT = 120.0


def _run(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = False,
    timeout: float = QUERY_TIMEOUT,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        timeout=timeout,
        text=True,
        input=input,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _base(kubeconfig: Path) -> list[str]:
    return [BIN, "--kubeconfig", str(kubeconfig)]


def release(kubeconfig: Path, name: str, namespace: str) -> dict | None:
    """The installed release's `helm list` record, or None when not installed."""
    proc = _run(
        _base(kubeconfig)
        + ["list", "--namespace", namespace, "--filter", f"^{re.escape(name)}$", "--output", "json"]
    )
    if proc.returncode != 0:
        raise ReconcileError(f"helm list failed: {proc.stderr.strip()}")
    for item in json.loads(proc.stdout or "[]"):
        if item.get("name") == name:
            return item
    return None


def chart_version(record: dict) -> str:
    """The chart version of a `helm list` record ("cert-manager-v1.21.2" -> "v1.21.2")."""
    chart = str(record.get("chart") or "")
    base, sep, version = chart.rpartition("-")
    if sep and (version[:1].isdigit() or (version[:1] in "vV" and version[1:2].isdigit())):
        return version
    return chart


def get_values(kubeconfig: Path, name: str, namespace: str) -> dict:
    """The release's user-supplied values (`helm get values -o json`)."""
    proc = _run(
        _base(kubeconfig)
        + ["get", "values", name, "--namespace", namespace, "--output", "json"]
    )
    if proc.returncode != 0:
        raise ReconcileError(f"helm get values {name} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) or {}


def latest_version(chart: str, repo: str) -> str | None:
    """The newest chart version in `repo` (read-only, safe under dry-run)."""
    proc = _run([BIN, "show", "chart", chart, "--repo", repo], timeout=QUERY_TIMEOUT)
    if proc.returncode != 0:
        raise ReconcileError(f"helm show chart {chart} failed: {proc.stderr.strip()}")
    version = (yaml.safe_load(proc.stdout) or {}).get("version")
    return str(version) if version else None


def upgrade_install(
    kubeconfig: Path,
    name: str,
    chart: str,
    repo: str,
    namespace: str,
    version: str,
    values_yaml: str,
) -> None:
    """helm upgrade --install with values on stdin (create the namespace too)."""
    args = _base(kubeconfig) + [
        "upgrade",
        "--install",
        name,
        chart,
        "--repo",
        repo,
        "--namespace",
        namespace,
        "--create-namespace",
        "-f",
        "-",
    ]
    pinned = version not in ("", "latest")
    if pinned:
        args += ["--version", version]
    if dry_run():
        suffix = f" --version {version}" if pinned else ""
        action(f"helm upgrade --install {name} {chart} --repo {repo} "
               f"--namespace {namespace}{suffix}")
        return
    action(f"installing chart {chart} as release {name} (namespace {namespace})")
    proc = _run(args, timeout=UPGRADE_TIMEOUT, input=values_yaml)
    if proc.returncode != 0:
        raise ReconcileError(f"helm upgrade --install {name} failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)


def uninstall(kubeconfig: Path, name: str, namespace: str) -> None:
    """helm uninstall; the caller checks the release exists first."""
    args = _base(kubeconfig) + ["uninstall", name, "--namespace", namespace]
    if dry_run():
        action(f"helm uninstall {name} --namespace {namespace}")
        return
    action(f"uninstalling release {name} (namespace {namespace})")
    proc = _run(args, timeout=UPGRADE_TIMEOUT)
    if proc.returncode != 0:
        raise ReconcileError(f"helm uninstall {name} failed: {proc.stderr.strip()}")
    for line in proc.stdout.splitlines():
        info(line)
