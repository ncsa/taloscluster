"""Standalone `taloscluster plugin argocd converge|plan` applies validation first.

Top-level converge validates every installed plugin's configuration in its
validate phase, before any cluster change. A plugin run on its own
(`taloscluster plugin NAME [converge|plan]`) previously called the hook
directly, so a lone repository URL or an unsupported version override was
silently applied instead of refused up front. These go through the real CLI
entry point and the real argocd plugin, so `validate_argocd` runs before the
mutating/planning hook and the run is refused with a non-zero exit.
"""

from __future__ import annotations

import yaml
from taloscluster import cli

MINIMAL = {
    "name": "testcluster",
    "talos": {"version": "v1.13.9"},
    "kubernetes": {"version": "v1.31.0"},
    "controlplane": {"count": 3, "flavor": "gp.medium", "disk": 40},
    "openstack": {
        "url": "https://example.com:5000/v3/",
        "availability_zone": "nova",
        "external_net": "ext-net",
    },
    "network": {
        "cidr": "192.168.0.0/21",
        "dns": ["1.1.1.1"],
        "ntp": ["ntp.example.com"],
    },
}


def _write(root, argocd_cluster, argocd_secrets=None):
    cluster = dict(MINIMAL)
    cluster["argocd"] = argocd_cluster
    root.mkdir(parents=True, exist_ok=True)
    (root / "cluster.yaml").write_text(yaml.safe_dump(cluster))
    secrets = {}
    if argocd_secrets is not None:
        secrets["argocd"] = argocd_secrets
    (root / "secrets.yaml").write_text(yaml.safe_dump(secrets))


def _run(action, root):
    return cli.main(["plugin", "-C", str(root), "argocd", action])


def _write_and_run(action, root, argocd_cluster, argocd_secrets=None):
    _write(root, argocd_cluster, argocd_secrets)
    return _run(action, root)


def test_converge_refuses_a_lone_repository_url(tmp_path, capsys):
    """A lone git.url would render incomplete resources, so convergence is
    refused before the hook runs."""
    rc = _write_and_run(
        "converge", tmp_path,
        argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    assert rc == 1
    assert "infra.url must be set together" in capsys.readouterr().err


def test_plan_refuses_a_lone_repository_url(tmp_path, capsys):
    rc = _write_and_run(
        "plan", tmp_path,
        argocd_cluster={"git": {"url": "https://git.example.com/cluster.git"}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    assert rc == 1
    assert "infra.url must be set together" in capsys.readouterr().err


def test_converge_refuses_an_unsupported_version_override(tmp_path, capsys):
    """A version the renderer would silently ignore is refused before planning."""
    rc = _write_and_run(
        "converge", tmp_path,
        argocd_cluster={"monitoring": {"enabled": True, "version": "1.0"}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    assert rc == 1
    assert "does not forward a chart version" in capsys.readouterr().err


def test_plan_refuses_an_unsupported_version_override(tmp_path, capsys):
    rc = _write_and_run(
        "plan", tmp_path,
        argocd_cluster={"monitoring": {"enabled": True, "version": "1.0"}},
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    assert rc == 1
    assert "does not forward a chart version" in capsys.readouterr().err


def test_converge_refuses_an_unsupported_apply_target(tmp_path, capsys):
    """A url/token apply target without a kubeconfig/context is unsupported and
    must be reported before the mutating hook instead of silently skipping."""
    rc = _write_and_run(
        "converge", tmp_path,
        argocd_cluster={},
        argocd_secrets={"url": "https://argocd.example.edu", "token": "CHANGE-ME"},
    )
    assert rc == 1
    assert "not a supported apply target" in capsys.readouterr().err


def test_plan_runs_a_valid_config(tmp_path):
    """Passing validation is a precondition, not a block: a valid configuration
    still reaches the converge hook, which on an un-bootstrapped cluster defers
    the registration and stays a successful no-op."""
    rc = _write_and_run(
        "plan", tmp_path,
        argocd_cluster={
            "git": {"url": "https://git.example.com/cluster.git"},
            "infra": {"url": "https://git.example.com/infra.git"},
        },
        argocd_secrets={"kubeconfig": "../argocd-kubeconfig"},
    )
    assert rc == 0
