"""Upstream version lookups: what the newest Talos / Kubernetes releases are.

Two public sources, both unauthenticated and already trusted by the rest of the
tool:

  factory.talos.dev/versions   every talos version the image factory can build
                               (so a version listed here is one we could
                               actually boot), including pre-releases
  dl.k8s.io/release/stable.txt the newest stable kubernetes, plus
                               stable-<minor>.txt for the newest patch of a
                               given minor

Pre-releases (``v1.14.0-beta.1``) are filtered out of "latest": taloscluster
never suggests running an alpha/beta/rc on a real cluster.
"""

from __future__ import annotations

import requests

FACTORY_VERSIONS = "https://factory.talos.dev/versions"
K8S_STABLE = "https://dl.k8s.io/release/stable.txt"
K8S_STABLE_MINOR = "https://dl.k8s.io/release/stable-{minor}.txt"

TIMEOUT = 15


def _get(url: str) -> str:
    """GET a small text document, or raise requests.RequestException."""
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text.strip()


# ---- parsing / comparison -------------------------------------------------

def parse(version: str) -> tuple[int, ...]:
    """"v1.13.8" -> (1, 13, 8).

    Pre-release and build metadata are dropped, so "v1.14.0-beta.1" and the
    "v1.35.2+talos" a kubelet reports both parse as their base version -- use
    `is_stable` to tell a pre-release apart. Non-numeric junk yields ().
    """
    parts = version.lstrip("v").split("-", 1)[0].split("+", 1)[0].split(".")
    out = []
    for p in parts:
        if not p.isdigit():
            break
        out.append(int(p))
    return tuple(out)


def is_stable(version: str) -> bool:
    return "-" not in version.lstrip("v")


def minor(version: str) -> str:
    """"v1.36.1" -> "1.36" (the form dl.k8s.io's stable-<minor>.txt wants)."""
    p = parse(version)
    if len(p) < 2:
        raise ValueError(f"not a x.y.z version: {version!r}")
    return f"{p[0]}.{p[1]}"


def is_older(a: str, b: str) -> bool:
    """True if `a` is an older release than `b` (empty/unparseable => False)."""
    pa, pb = parse(a), parse(b)
    if not pa or not pb:
        return False
    return pa < pb


# ---- talos ----------------------------------------------------------------

def talos_versions() -> list[str]:
    """Every version the factory can build, newest last, pre-releases included."""
    resp = requests.get(FACTORY_VERSIONS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected payload from {FACTORY_VERSIONS}")
    return sorted((str(v) for v in data), key=lambda v: (parse(v), is_stable(v)))


def latest_talos(versions: list[str] | None = None) -> str:
    """Newest stable talos release."""
    stable = [v for v in (versions if versions is not None else talos_versions())
              if is_stable(v)]
    if not stable:
        raise ValueError("no stable talos versions returned by the factory")
    return stable[-1]


def latest_talos_patch(want_minor: str, versions: list[str] | None = None) -> str:
    """Newest stable patch of one talos minor, e.g. "1.13" -> "v1.13.9".

    Returns "" if that minor has no stable release (a minor that only exists as
    alphas yet).
    """
    prefix = parse(f"v{want_minor}")
    stable = [v for v in (versions if versions is not None else talos_versions())
              if is_stable(v) and parse(v)[:2] == prefix[:2]]
    return stable[-1] if stable else ""


# ---- kubernetes -----------------------------------------------------------

def latest_kubernetes() -> str:
    """Newest stable kubernetes release, e.g. "v1.36.4"."""
    return _get(K8S_STABLE)


def latest_kubernetes_patch(want_minor: str) -> str:
    """Newest patch release of a kubernetes minor, e.g. "1.35" -> "v1.35.7"."""
    return _get(K8S_STABLE_MINOR.format(minor=want_minor))
