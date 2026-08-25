"""taloscluster argocd plugin -- register the cluster with ArgoCD.

Renders and applies, to the ArgoCD cluster, the manifests that let ArgoCD manage
this cluster: the cluster Secret (built from this cluster's own kubeconfig), the
AppProject with its admin/user roles, the git repository Secret, the root
Application and the per-cluster app-of-apps.

Installed as `taloscluster[argocd]`; taloscluster discovers it through the
`taloscluster.plugins` entry point and runs it as part of converge / plan /
destroy / status / check. This module is the entry point, so it re-exports the
plugin protocol.
"""

from __future__ import annotations

from taloscluster.context import Context

from .config import argocd_configured
from .reconcile import check, converge, destroy, status

__version__ = "0.1.0"

# rancher first when it is installed: its converge publishes the Rancher cluster
# id, which the cluster Secret gets annotated with. Ignored when rancher is not
# installed -- this is an ordering wish, not a dependency.
AFTER: tuple[str, ...] = ("rancher",)

__all__ = ["AFTER", "check", "configured", "converge", "destroy", "status"]


def configured(ctx: Context) -> bool:
    """True when secrets.yaml carries an `argocd:` apply target -- any one of a
    kubeconfig, a kubectl context, or url + token."""
    return argocd_configured(ctx.root)
