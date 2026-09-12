"""ArgoCD activation matches supported connection modes.

Only a kubectl apply target (kubeconfig/context) activates the plugin. A
`url`/`token` pair alone names an ArgoCD API endpoint the plugin cannot talk to,
so it must not show as configured only to fail in every hook.
"""

from __future__ import annotations

import yaml

from taloscluster_argocd.config import argocd_configured


def _write_secrets(root, argocd):
    root.mkdir(parents=True, exist_ok=True)
    (root / "secrets.yaml").write_text(yaml.safe_dump({"argocd": argocd}))


def test_url_token_alone_does_not_activate(tmp_path):
    _write_secrets(tmp_path, {"url": "https://argocd.example.edu", "token": "CHANGE-ME"})
    assert argocd_configured(tmp_path) is False


def test_kubeconfig_activates(tmp_path):
    _write_secrets(tmp_path, {"kubeconfig": "../argocd-kubeconfig"})
    assert argocd_configured(tmp_path) is True


def test_context_activates(tmp_path):
    _write_secrets(tmp_path, {"context": "argocd"})
    assert argocd_configured(tmp_path) is True


def test_kubectl_mode_with_url_token_still_activates(tmp_path):
    _write_secrets(
        tmp_path,
        {"kubeconfig": "../argocd-kubeconfig", "url": "https://argocd.example.edu", "token": "x"},
    )
    assert argocd_configured(tmp_path) is True


def test_empty_argocd_section_does_not_activate(tmp_path):
    _write_secrets(tmp_path, {})
    assert argocd_configured(tmp_path) is False
