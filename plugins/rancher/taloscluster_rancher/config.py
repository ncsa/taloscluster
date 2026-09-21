"""Load + validate the `rancher:` section of the merged configuration.

cluster.yaml (committed) — members are NCSA netids/usernames (not emails):
    rancher:
      admins: [alice, bob]   # -> cluster-owner
      users:  [carol]        # -> cluster-member

secrets.yaml (gitignored):
    rancher:
      url:   https://rancher.example.edu
      token: token-xxxxx:yyyyyyyyyyyy

The two files (plus any `include:`) are merged before the section is read, so a
value may live in any of them; the split above is only the scaffolded default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from taloscluster.config import CLUSTER_FILE, load_raw, require
from taloscluster.errors import ConfigError

# Rancher roleTemplateId for each membership tier.
ROLE_BY_TIER = {
    "admins": "cluster-owner",
    "users": "cluster-member",
}

# Direct keys the `rancher:` section accepts, wherever it lives; anything else
# is a typo or an unsupported option and is refused rather than silently
# dropped.
_RANCHER_KEYS = {"admins", "users", "url", "token"}


def rancher_configured(root: Path) -> bool:
    """True only when the cluster opts into Rancher management -- a `rancher:`
    section in cluster.yaml or an included file -- and the merged configuration
    carries url + token, wherever they were written.

    A section that only secrets.yaml carries supplies credentials but does not
    switch the feature on, matching how core treats credentials-only sections;
    without the opt-in or the credentials this cluster is not managed by Rancher
    and the tool should do nothing."""
    try:
        raw, opted_in = load_raw(root)
    except ConfigError:
        return False
    if "rancher" not in opted_in:
        return False
    rancher = raw.get("rancher")
    return isinstance(rancher, dict) and "url" in rancher and "token" in rancher


def validate_rancher(root: Path) -> None:
    """Refuse a malformed or contradictory `rancher:` configuration.

    Called by core in converge's validate phase, before any cluster mutation, so
    a broken `rancher:` section (a non-mapping section, admins/users that are not
    lists of usernames, secret credential values that are not non-empty strings,
    or a member listed under both tiers) stops the run while the cluster is
    still untouched instead of failing the late reconcile hooks. The hook runs
    whether or not the plugin is active, so a supplied-but-malformed `rancher:`
    section is rejected even though activation would otherwise discard it; an
    entirely absent section passes. The section is read from the merged
    configuration, so it may live in cluster.yaml, secrets.yaml or any included
    file, and a problem is reported against cluster.yaml whichever file supplied
    it. Raises ConfigError on the first problem.
    """
    raw, _opted_in = load_raw(root)
    rancher_raw: Any = raw.get("rancher")
    if rancher_raw is not None and not isinstance(rancher_raw, dict):
        raise ConfigError(f"{CLUSTER_FILE}: rancher must be a YAML mapping")
    rancher = rancher_raw if isinstance(rancher_raw, dict) else {}

    # a miscapped or unsupported key inside the `rancher:` section is refused
    # rather than silently ignored (the top-level core allowlist only sees the
    # `rancher:` key itself, which the plugin owns).
    unknown = sorted(set(rancher) - _RANCHER_KEYS)
    if unknown:
        raise ConfigError(
            f"{CLUSTER_FILE} (rancher): unsupported option(s): {', '.join(unknown)}; "
            "the plugin does not use them"
        )

    # member role lists must be lists of usernames. A bare string would iterate
    # character by character when the reconciler flattens a tier.
    for role in ("admins", "users"):
        members = rancher.get(role)
        if members is not None and (
            not isinstance(members, list)
            or any(not isinstance(m, str) or not m.strip() for m in members)
        ):
            raise ConfigError(
                f"{CLUSTER_FILE} (rancher.{role}) must be a list of usernames"
            )

    admins = tuple(rancher.get("admins", []) or [])
    users = tuple(rancher.get("users", []) or [])
    overlap = sorted(set(admins) & set(users))
    if overlap:
        raise ConfigError(
            f"{CLUSTER_FILE} (rancher): member(s) listed under both 'admins' and "
            f"'users': {', '.join(overlap)}; a membership tier is ambiguous, "
            "list each member under exactly one of the two"
        )

    # the url/token feed the Rancher HTTP client, so they must be non-empty
    # strings. A null or non-string credential would load cleanly and then crash
    # the late HTTP client (NoneType.rstrip) after core mutation, matching the
    # core _secret precedent of refusing null credentials up front. Only a key
    # that is present is checked, so an absent key (an inactive section) passes.
    for key in ("url", "token"):
        if key not in rancher:
            continue
        value = rancher.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"{CLUSTER_FILE} (rancher.{key}) must be a non-empty string"
            )


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
        raw, _opted_in = load_raw(root)
        where = CLUSTER_FILE
        rancher = raw.get("rancher", {}) or {}
        admins = tuple(rancher.get("admins", []) or [])
        users = tuple(rancher.get("users", []) or [])
        overlap = sorted(set(admins) & set(users))
        if overlap:
            raise ConfigError(
                f"{where} (rancher): member(s) listed under both 'admins' and "
                f"'users': {', '.join(overlap)}; a membership tier is ambiguous, "
                "list each member under exactly one of the two"
            )
        members = Members(
            admins=admins,
            users=users,
        )
        return cls(
            name=require(raw, "name", where=where),
            members=members,
        )

    @classmethod
    def load_secrets(cls, root: Path) -> Secrets:
        """The Rancher credentials, from wherever in the merged configuration
        they were written (secrets.yaml, cluster.yaml or an included file)."""
        raw, _opted_in = load_raw(root)
        rancher = raw.get("rancher", {}) or {}
        return Secrets(
            rancher_url=require(rancher, "url", where=f"{CLUSTER_FILE} (rancher)"),
            rancher_token=require(rancher, "token", where=f"{CLUSTER_FILE} (rancher)"),
        )
