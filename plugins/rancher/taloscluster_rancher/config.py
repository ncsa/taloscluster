"""Load + validate the `rancher:` section of cluster.yaml and secrets.yaml.

cluster.yaml (committed) — members are NCSA netids/usernames (not emails):
    rancher:
      admins: [alice, bob]   # -> cluster-owner
      users:  [carol]        # -> cluster-member

secrets.yaml (gitignored):
    rancher:
      url:   https://gonzo-rancher.ncsa.illinois.edu
      token: token-xxxxx:yyyyyyyyyyyy
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE, read_yaml, require
from taloscluster.errors import ConfigError

# Rancher roleTemplateId for each membership tier.
ROLE_BY_TIER = {
    "admins": "cluster-owner",
    "users": "cluster-member",
}


def rancher_configured(root: Path) -> bool:
    """True only when a `rancher:` section exists in BOTH cluster.yaml and
    secrets.yaml (the latter with url + token).

    A missing section in either file means this cluster is not managed by
    Rancher and the tool should do nothing."""
    try:
        dc = read_yaml(root / CLUSTER_FILE)
        ds = read_yaml(root / SECRETS_FILE)
    except ConfigError:
        return False
    if "rancher" not in dc or not isinstance(dc.get("rancher"), dict):
        return False
    rancher_s = ds.get("rancher")
    if not isinstance(rancher_s, dict):
        return False
    return "url" in rancher_s and "token" in rancher_s


@dataclass(frozen=True)
class Members:
    """Desired members, tier -> list of NCSA netids/usernames."""

    admins: tuple[str, ...]
    users: tuple[str, ...]

    def netids_for(self, tier: str) -> tuple[str, ...]:
        return getattr(self, tier)


@dataclass(frozen=True)
class Secrets:
    rancher_url: str
    rancher_token: str


@dataclass
class Config:
    name: str
    members: Members

    @classmethod
    def load(cls, root: Path) -> Config:
        d = read_yaml(root / CLUSTER_FILE)
        where = CLUSTER_FILE
        rancher = d.get("rancher", {}) or {}
        members = Members(
            admins=tuple(rancher.get("admins", []) or []),
            users=tuple(rancher.get("users", []) or []),
        )
        return cls(
            name=require(d, "name", where=where),
            members=members,
        )

    @classmethod
    def load_secrets(cls, root: Path) -> Secrets:
        d = read_yaml(root / SECRETS_FILE)
        where = SECRETS_FILE
        rancher = d.get("rancher", {}) or {}
        return Secrets(
            rancher_url=require(rancher, "url", where=f"{where} (rancher)"),
            rancher_token=require(rancher, "token", where=f"{where} (rancher)"),
        )
