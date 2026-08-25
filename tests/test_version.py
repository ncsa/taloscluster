"""Package metadata is the single source of truth for the CLI version."""

from importlib.metadata import version

import taloscluster_argocd
import taloscluster_rancher

import taloscluster


def test_runtime_version_comes_from_package_metadata():
    assert taloscluster.__version__ == version("taloscluster")
    assert taloscluster_argocd.__version__ == version("taloscluster-argocd")
    assert taloscluster_rancher.__version__ == version("taloscluster-rancher")
