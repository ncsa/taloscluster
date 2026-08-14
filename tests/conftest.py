"""Shared fixtures for the clusterctl test suite.

Pure unit tests: no cloud access, no talosctl/kubectl binaries. The
``make_config`` fixture exercises the real :func:`load_config` loader by
writing a ``cluster.yaml`` to ``tmp_path`` built from a dict of overrides
merged on top of a minimal valid cluster.yaml-shaped dict.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from clusterctl.config import load_config

# A minimal but fully valid cluster.yaml shape. Every key ``load_config``
# hard-requires is present; pools carry count/flavor/disk so ``_validate``
# passes. Tests override or remove pieces to exercise specific behaviour.
MINIMAL: dict[str, Any] = {
    "name": "testcluster",
    "talos": {"version": "v1.8.3"},
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


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``; dicts merged, scalars/lists replaced."""
    out = {k: v for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@pytest.fixture
def make_config(tmp_path: Path):
    """Return a callable that builds a :class:`Config` from overrides.

    ``overrides`` are deep-merged on top of :data:`MINIMAL`. ``remove`` is a
    sequence of dotted key paths to delete after merging (used to exercise
    missing-key errors). The merged dict is written to ``tmp_path/cluster.yaml``
    and loaded through the real :func:`load_config`, so validation runs.
    """

    def _build(overrides: dict[str, Any] | None = None, *, remove: tuple[str, ...] = ()) -> Any:
        # deepcopy so the `remove` step (and deep-merge) never mutates MINIMAL,
        # which would pollute later tests in the same session.
        merged = _deep_merge(copy.deepcopy(MINIMAL), overrides or {})
        for path in remove:
            parts = path.split(".")
            d: dict[str, Any] = merged
            for p in parts[:-1]:
                d = d.get(p, {})
            d.pop(parts[-1], None)
        (tmp_path / "cluster.yaml").write_text(yaml.safe_dump(merged))
        return load_config(tmp_path)

    return _build
