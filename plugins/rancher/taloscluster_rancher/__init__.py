"""taloscluster rancher plugin -- register the cluster and reconcile its members.

Reads the `rancher:` section of cluster.yaml (admins/users as NCSA netid lists)
plus rancher.url / rancher.token from secrets.yaml, and makes Rancher match: the
cluster is imported if missing (agent installed via kubectl), and every member is
added with the right role (admin -> cluster-owner, user -> cluster-member).

Installed as `taloscluster[rancher]`; taloscluster discovers it through the
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

from .config import rancher_configured
from .reconcile import check, converge, destroy, status

try:
    __version__ = version("taloscluster-rancher")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"

# nothing to wait for: rancher runs first and hands its cluster id to whoever
# declares AFTER = ("rancher",)
AFTER: tuple[str, ...] = ()

CLUSTER_SCAFFOLD = """\
# Rancher membership uses usernames/netids.
rancher:
  admins: []
  users: []
"""

SECRETS_SCAFFOLD = """\
# Add both values to activate Rancher registration.
rancher:
  # url: https://rancher.example.com
  # token: token-xxxxx:yyyyyyyyyyyy
"""

__all__ = ["AFTER", "check", "configured", "converge", "destroy", "init", "status"]


def init(root: Path) -> None:
    """Add inactive starter Rancher sections without replacing existing config."""
    add_yaml_section(root / CLUSTER_FILE, "rancher", CLUSTER_SCAFFOLD)
    add_yaml_section(root / SECRETS_FILE, "rancher", SECRETS_SCAFFOLD)


def configured(ctx: Context) -> bool:
    """True only when BOTH cluster.yaml and secrets.yaml carry a `rancher:`
    section (the latter with url + token). A missing section in either means this
    cluster is not managed by Rancher and the plugin does nothing."""
    return rancher_configured(ctx.root)
