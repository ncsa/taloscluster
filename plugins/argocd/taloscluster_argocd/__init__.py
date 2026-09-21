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

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE
from taloscluster.context import Context
from taloscluster.scaffold import add_yaml_section

from .config import argocd_configured, validate_argocd
from .reconcile import check, converge, destroy, status

try:
    __version__ = version("taloscluster-argocd")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"

# rancher first when it is installed: its converge publishes the Rancher cluster
# id, which the cluster Secret gets annotated with. Ignored when rancher is not
# installed -- this is an ordering wish, not a dependency.
AFTER: tuple[str, ...] = ("rancher",)

# Top-level cluster.yaml / secrets.yaml keys this plugin owns. Core retains them
# as valid config keys when this plugin is installed, so an argocd: section is
# not reported as an unknown top-level key.
CONFIG_SECTIONS: tuple[str, ...] = ("argocd",)

CLUSTER_SCAFFOLD = """\
# ArgoCD project access uses full email addresses.
argocd:
  admins: []
  users: []
  git:
    url: https://git.example.com/kubernetes/cluster.git
  infra:
    url: https://git.example.com/kubernetes/infra.git # charts/apps app-of-apps chart
"""

SECRETS_SCAFFOLD = """\
# Choose a kubeconfig path or kubectl context for the ArgoCD cluster.
argocd:
  # kubeconfig: ../argocd-kubeconfig
  # context: argocd
"""

__all__ = ["AFTER", "check", "configured", "converge", "destroy", "init", "status", "validate"]


def init(root: Path) -> None:
    """Add inactive starter ArgoCD sections without replacing existing config."""
    add_yaml_section(root / CLUSTER_FILE, "argocd", CLUSTER_SCAFFOLD)
    add_yaml_section(root / SECRETS_FILE, "argocd", SECRETS_SCAFFOLD)


def configured(ctx: Context) -> bool:
    """True when the merged configuration carries an `argocd:` kubectl apply
    target (a kubeconfig or a context), wherever it was written. A `url`/`token`
    pair alone does not activate the plugin: the plugin cannot apply through the
    ArgoCD API."""
    return argocd_configured(ctx.root)


def validate(root: Path, ctx: Context) -> None:
    """Reject a malformed or contradictory `argocd:` configuration.

    Runs in core's converge validate phase, before any cluster mutation, so a
    broken `argocd:` section (paired repository URLs, git credentials without a
    Git URL, a non-mapping section, an unsupported top-level or per-app option,
    a chart `version` the plugin would silently ignore, or a `url`/`token`
    apply target without a `kubeconfig`/`context`) stops the run while the
    cluster is still untouched instead of failing the late plugin hooks. The
    hook runs whether or not the plugin is active, so a non-mapping or
    unsupported-mode `argocd:` section is rejected even though activation would
    otherwise silently discard it.
    """
    validate_argocd(root)
