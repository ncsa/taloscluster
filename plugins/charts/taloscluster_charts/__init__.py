"""taloscluster charts plugin -- install helm charts into this cluster on sync.

Converges the `charts:` section of cluster.yaml: each entry is one helm chart
(or manifest set) installed into this cluster whenever the cluster is synced.
Charts the plugin knows by name (gateway, metallb, traefik) ship common values
and defaults; values taloscluster already knows -- the metallb ingress pool --
come from the plugin Context, never from config.

Installed as `taloscluster[charts]`; taloscluster discovers it through the
`taloscluster.plugins` entry point and runs it as part of converge / plan /
destroy / status / check. This module is the entry point, so it re-exports
the plugin protocol.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE
from taloscluster.context import Context
from taloscluster.scaffold import add_yaml_section

from .config import charts_configured, validate_charts
from .reconcile import check, converge, destroy, status

try:
    __version__ = version("taloscluster-charts")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"

# Top-level cluster.yaml key this plugin owns. Core retains it as a valid key
# while the plugin is installed, so a `charts:` section is not reported as an
# unknown top-level key. The ceph credentials live under charts.ceph too.
CONFIG_SECTIONS: tuple[str, ...] = ("charts",)

# every known chart, scaffolded disabled: `taloscluster init` adds the section,
# and flipping `enabled` is the whole activation story
CLUSTER_SCAFFOLD = """\
# Charts installed into this cluster on sync. Each entry is one helm chart (or
# manifest set); the plugin ships common values and `values:` overrides them.
# Known charts are listed disabled -- flip enabled to turn one on.
charts:
  ceph:
    enabled: false       # ceph storage: rbd and/or cephfs; needs clusterID + monitors
    # clusterID: <ceph-fsid>            # from `ceph fsid`
    # monitors: [mon.example.edu:6789]  # from `ceph mon dump`
    # userID/userKey: the CephX credentials, scaffolded in secrets.yaml
    rbd: true            # installs the ceph-csi-rbd chart; or a mapping with the
    #   pool (and defaultClass, name, reclaimPolicy, ...) to also create a StorageClass
    fs: true             # installs the ceph-csi-cephfs chart; or a mapping with fsName
    version: latest
  cert-manager:
    enabled: false        # TLS certificate provisioning
    # email: acme@example.edu   # required when staging/prod is on
    staging: false        # adds a letsencrypt-staging ClusterIssuer
    prod: true            # adds a letsencrypt-prod ClusterIssuer
    version: latest
  nfs:
    enabled: false        # needs a storageClasses list (server/share per class)
    # storageClasses:
    # - name: nfs-data
    #   server: nfs.example.edu
    #   share: /exports/data
    #   defaultClass: true
    version: latest
  gateway:
    enabled: false        # Gateway API CRDs; traefik's gateway provider needs these first
    version: latest       # newest gateway-api release; or pin a tag (e.g. v1.6.2)
  metallb:
    enabled: false        # gets the external ingress pool from cluster.yaml automatically
    version: latest
  sealed-secrets:
    enabled: false        # controller for Bitnami SealedSecrets
    version: latest
  traefik:
    enabled: false        # serves certs from ingress tls secrets (issued by cert-manager)
    version: latest
"""

# CephX credentials the ceph-csi provisioners use; delivered as csi-rbd-secret
# and csi-cephfs-secret by the charts plugin. Optional -- manage the secrets
# yourself (sealed-secrets, kubectl) and leave this commented out. Merges with
# the charts.ceph entry in cluster.yaml.
SECRETS_SCAFFOLD = """\
# charts:
#   ceph:
#     userID: kubernetes   # a client.<userID> made with `ceph auth get-or-create`
#     userKey: AQC...      # from `ceph auth get-key client.<userID>`
"""

__all__ = ["check", "configured", "converge", "destroy", "init", "status", "validate"]


def init(root: Path) -> None:
    """Add inactive starter sections without replacing existing config."""
    add_yaml_section(root / CLUSTER_FILE, "charts", CLUSTER_SCAFFOLD)
    # core's init writes secrets.yaml before the plugin fan-out; skip the
    # scaffold when it does not exist rather than creating it half-formed
    if (root / SECRETS_FILE).is_file():
        add_yaml_section(root / SECRETS_FILE, "charts", SECRETS_SCAFFOLD)


def configured(ctx: Context) -> bool:
    """True when cluster.yaml carries a `charts:` mapping."""
    return charts_configured(ctx.root)


def validate(root: Path, ctx: Context) -> None:
    """Reject a malformed or contradictory `charts:` section.

    Runs in core's converge validate phase, before any cluster mutation, so a
    broken section (an unsupported key, a chart with both repo and manifest, a
    traefik without its email) stops the run while the cluster is still
    untouched instead of failing the late plugin hooks.
    """
    validate_charts(root)
