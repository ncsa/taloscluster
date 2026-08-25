"""taloscluster -- converge a Talos cluster on OpenStack from cluster.yaml.

A pure-Python alternative to the terraform/ + bin/cluster.sh workflow living in
the same folder. Both provision the same thing; only one is used per cluster.
See README-python.md.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("taloscluster")
except PackageNotFoundError:  # source tree imported without installing the package
    __version__ = "0+unknown"
