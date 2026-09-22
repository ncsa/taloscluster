"""Upstream version lookups for manifest-managed components.

The gateway entry pins a gateway-api release; `check` reports when a newer
release exists so a version bump is a conscious act rather than a surprise
(manifest entries are never auto-upgraded).
"""

from __future__ import annotations

import requests

GATEWAY_LATEST = "https://api.github.com/repos/kubernetes-sigs/gateway-api/releases/latest"


def gateway_latest_version(timeout: float = 10.0) -> str | None:
    """The newest gateway-api release tag ("v1.6.2"), or None when unknown.

    Best effort: a network failure or rate limit must not fail `check` -- the
    upgrade note is informational.
    """
    try:
        response = requests.get(
            GATEWAY_LATEST,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        tag = response.json().get("tag_name")
        return str(tag) if tag else None
    except (requests.RequestException, ValueError):
        return None
