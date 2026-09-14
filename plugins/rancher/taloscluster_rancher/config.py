"""Load + validate the `rancher:` section of cluster.yaml and secrets.yaml.

cluster.yaml (committed) — members are NCSA netids/usernames (not emails):
    rancher:
      admins: [alice, bob]   # -> cluster-owner
      users:  [carol]        # -> cluster-member

secrets.yaml (gitignored):
    rancher:
      url:   https://rancher.example.edu
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

# Direct keys the `rancher:` section of each file accepts; anything else is a
# typo or an unsupported option and is refused rather than silently dropped.
_RANCHER_CLAN_KEYS = {"admins", "users"}
_RANCHER_SECRETS_KEYS = {"url", "token"}


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


def validate_rancher(root: Path) -> None:
    """Refuse a malformed or contradictory `rancher:` configuration.

    Called by core in converge's validate phase, before any cluster mutation, so
    a broken `rancher:` section (a non-mapping section, admins/users that are not
    lists of usernames, secret credential values that are not non-empty strings,
    or a member listed under both tiers) stops the run while the cluster is
    still untouched instead of failing the late reconcile hooks. The hook runs
    whether or not the plugin is active, so a supplied-but-malformed `rancher:`
    section is rejected even though activation would otherwise discard it; an
    entirely absent section passes. A missing config file is treated as absent
    configuration (nothing supplied to validate), matching how activation
    already tolerates a missing file; core enforces that the files exist for a
    real converge. Raises ConfigError on the first problem.
    """
    dc = read_yaml(root / CLUSTER_FILE) if (root / CLUSTER_FILE).is_file() else {}
    ds = read_yaml(root / SECRETS_FILE) if (root / SECRETS_FILE).is_file() else {}
    clan_raw = dc.get("rancher")
    sec_raw = ds.get("rancher")
    if clan_raw is not None and not isinstance(clan_raw, dict):
        raise ConfigError(f"{CLUSTER_FILE}: rancher must be a YAML mapping")
    if sec_raw is not None and not isinstance(sec_raw, dict):
        raise ConfigError(f"{SECRETS_FILE}: rancher must be a YAML mapping")
    clan = clan_raw if isinstance(clan_raw, dict) else {}
    sec = sec_raw if isinstance(sec_raw, dict) else {}

    # a miscapped or unsupported key inside the `rancher:` section is refused
    # rather than silently ignored (the top-level core allowlist only sees the
    # `rancher:` key itself, which the plugin owns).
    for source, section, known in (
        (CLUSTER_FILE, clan, _RANCHER_CLAN_KEYS),
        (SECRETS_FILE, sec, _RANCHER_SECRETS_KEYS),
    ):
        unknown = sorted(set(section) - known)
        if unknown:
            raise ConfigError(
                f"{source} (rancher): unsupported option(s): {', '.join(unknown)}; "
                "the plugin does not use them"
            )

    # member role lists must be lists of usernames. A bare string would iterate
    # character by character when the reconciler flattens a tier.
    for role in ("admins", "users"):
        members = clan.get(role)
        if members is not None and (
            not isinstance(members, list)
            or any(not isinstance(m, str) or not m.strip() for m in members)
        ):
            raise ConfigError(
                f"{CLUSTER_FILE} (rancher.{role}) must be a list of usernames"
            )

    admins = tuple(clan.get("admins", []) or [])
    users = tuple(clan.get("users", []) or [])
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
        if key not in sec:
            continue
        value = sec.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(
                f"{SECRETS_FILE} (rancher.{key}) must be a non-empty string"
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
        d = read_yaml(root / CLUSTER_FILE)
        where = CLUSTER_FILE
        rancher = d.get("rancher", {}) or {}
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
