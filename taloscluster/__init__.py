"""taloscluster -- converge a Talos cluster from cluster.yaml.

A pure-Python tool that creates and maintains a Talos Kubernetes cluster on
OpenStack or Proxmox from a declarative `cluster.yaml` (plus a `secrets.yaml`).
See the documentation in `docs/`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("taloscluster")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"
