"""Load + validate the `argocd:` section of cluster.yaml and secrets.yaml.

cluster.yaml (committed) -- role members are full email addresses:
    argocd:
      admins: [alice@example.com]   # -> project 'admin' role
      users:  [carol@example.com]   # -> project 'user' role

These are merged with the `rancher:` members (if a rancher section exists), so the
AppProject ends up reflecting both the Rancher access and any extra ArgoCD-only
access.

secrets.yaml (gitignored) -- how to reach the ArgoCD cluster to apply changes.
The plugin applies via kubectl, so an apply target needs a kubeconfig path or a
kubectl context (uses the default kubeconfig, e.g. ~/.kube/config):
    argocd:
      kubeconfig: ../some-argocd-kubeconfig
      # or --
      context: argocd                   # kubectl --context (default kubeconfig)

A `url` / `token` pair alone is not a supported apply target: the plugin does not
speak the ArgoCD API, so it does not activate the plugin. See `argocd_configured`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from taloscluster.config import CLUSTER_FILE, SECRETS_FILE, read_yaml, require
from taloscluster.errors import ConfigError


def _load_openstack(d: dict[str, Any]) -> Openstack | None:
    """Non-secret OpenStack identity from cluster.yaml's `openstack:` section."""
    ost = _section(d, "openstack")
    if not ost:
        return None
    return Openstack(
        project=str(ost.get("project") or ""),
        url=str(ost.get("url") or ""),
        region=str(ost.get("region") or ""),
    )


@dataclass(frozen=True)
class Members:
    """Desired project roles, tier -> full email list."""

    admins: tuple[str, ...]
    users: tuple[str, ...]

    def emails_for(self, tier: str) -> tuple[str, ...]:
        return getattr(self, tier)


@dataclass(frozen=True)
class ApplyTarget:
    """Where/how manifests are applied to the ArgoCD cluster.

    - `kubeconfig`: a path (resolved against the cluster dir) used for kubectl.
    - `context`: optional `kubectl --context`; when kubeconfig is none, kubectl
      uses the default kubeconfig (e.g. ~/.kube/config).
    - `url` / `token`: ArgoCD API credentials (not a supported apply mode).
    """

    kubeconfig: str | None = None
    context: str | None = None
    url: str | None = None
    token: str | None = None
    git_username: str | None = None
    git_token: str | None = None
    openstack_credential_id: str | None = None
    openstack_credential_secret: str | None = None

    @property
    def uses_kubectl(self) -> bool:
        return self.kubeconfig is not None or self.context is not None


@dataclass(frozen=True)
class Openstack:
    """Non-secret OpenStack identity (from clusterctl status) for cluster-apps values."""

    project: str = ""
    url: str = ""
    region: str = ""


@dataclass
class Config:
    name: str
    members: Members
    git_url: str | None = None
    # git repo holding the charts/apps app-of-apps chart the cluster-apps Application points at
    infra_url: str | None = None
    openstack: Openstack | None = None
    metallb: dict[str, Any] = field(default_factory=dict)
    ingress: dict[str, Any] = field(default_factory=dict)
    sealedsecrets: dict[str, Any] = field(default_factory=dict)
    certmanager: dict[str, Any] = field(default_factory=dict)
    cinder: dict[str, Any] = field(default_factory=dict)
    nfs: dict[str, Any] = field(default_factory=dict)
    monitoring: dict[str, Any] = field(default_factory=dict)
    sync: bool = False
    # automatic sync, pruning, and self-healing on the two parent Applications
    automated: bool = True

    @classmethod
    def load(cls, root: Path) -> Config:
        d = read_yaml(root / CLUSTER_FILE)
        where = CLUSTER_FILE
        name = require(d, "name", where=where)
        clan = _section(d, "argocd")
        rancher = _section(d, "rancher")
        git = clan.get("git") or {}
        if not isinstance(git, dict):
            git = {}
        infra = clan.get("infra") or {}
        if not isinstance(infra, dict):
            infra = {}
        metallb = clan.get("metallb") or {}
        if not isinstance(metallb, dict):
            metallb = {}
        ingress = clan.get("ingress") or {}
        if not isinstance(ingress, dict):
            ingress = {}
        sealedsecrets = clan.get("sealedsecrets") or {}
        if not isinstance(sealedsecrets, dict):
            sealedsecrets = {}
        certmanager = clan.get("certmanager") or {}
        if not isinstance(certmanager, dict):
            certmanager = {}
        cinder = clan.get("cinder") or {}
        if not isinstance(cinder, dict):
            cinder = {}
        nfs = clan.get("nfs") or {}
        if not isinstance(nfs, dict):
            nfs = {}
        monitoring = clan.get("monitoring") or {}
        if not isinstance(monitoring, dict):
            monitoring = {}

        # project roles = rancher members (if present) merged with argocd members
        admins = _uniq(*clan.get("admins") or [], *rancher.get("admins") or [])
        users = _uniq(*clan.get("users") or [], *rancher.get("users") or [])
        return cls(
            name=name,
            members=Members(admins=admins, users=users),
            git_url=git.get("url"),
            infra_url=infra.get("url"),
            openstack=_load_openstack(d),
            metallb=metallb,
            ingress=ingress,
            sealedsecrets=sealedsecrets,
            certmanager=certmanager,
            cinder=cinder,
            nfs=nfs,
            monitoring=monitoring,
            sync=bool(clan.get("sync")),
            automated=clan.get("automated", True),
        )

    @classmethod
    def load_secrets(cls, root: Path) -> ApplyTarget:
        d = read_yaml(root / SECRETS_FILE)
        argocd = _section(d, "argocd")
        git = argocd.get("git") or {}
        git = git if isinstance(git, dict) else {}
        ost = _section(d, "openstack")
        return ApplyTarget(
            kubeconfig=argocd.get("kubeconfig"),
            context=argocd.get("context"),
            url=argocd.get("url"),
            token=argocd.get("token"),
            git_username=git.get("username"),
            git_token=git.get("token"),
            openstack_credential_id=ost.get("credential_id"),
            openstack_credential_secret=ost.get("credential_secret"),
        )


def _section(d: dict[str, Any], key: str) -> dict[str, Any]:
    section = d.get(key)
    return section if isinstance(section, dict) else {}


def enabled(section: dict[str, Any]) -> bool:
    """Whether an `argocd.<name>` section turns its feature on."""
    return bool(section.get("enabled"))


def _read_cluster(root: Path) -> dict[str, Any]:
    return read_yaml(root / CLUSTER_FILE) if (root / CLUSTER_FILE).is_file() else {}


def _read_secrets(root: Path) -> dict[str, Any]:
    return read_yaml(root / SECRETS_FILE) if (root / SECRETS_FILE).is_file() else {}


def _uniq(*values: str) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return tuple(out)


#: Top-level keys the `argocd:` section of cluster.yaml understands.
_KNOWN_KEYS = {
    "admins", "users", "git", "infra", "metallb", "ingress", "sealedsecrets",
    "certmanager", "cinder", "nfs", "monitoring", "sync", "automated",
}
#: `argocd:` sub-sections that must be YAML mappings (the per-app sections and
#: the git/infra repository blocks).
_MAPPING_KEYS = {
    "git", "infra", "metallb", "ingress", "sealedsecrets", "certmanager",
    "cinder", "nfs", "monitoring",
}
#: Keys each `argocd.<app>` per-app section accepts. Anything outside this set
#: is a typo or an unsupported override and is refused rather than silently
#: dropped.
_PER_APP_KEYS = {
    "metallb": {"enabled", "version"},
    "sealedsecrets": {"enabled", "version"},
    "certmanager": {"enabled", "version", "email"},
    "cinder": {"enabled", "version"},
    "ingress": {"enabled", "class", "traefik", "version"},
    "nfs": {"enabled", "servers", "version"},
    "monitoring": {"enabled", "version"},
}
#: Keys the `argocd:` secrets.yaml section accepts. Anything outside this set is
#: a miscapped or unsupported option and is refused rather than silently dropped.
_ARGOCD_SECRETS_KEYS = {"kubeconfig", "context", "url", "token", "git"}
#: Keys each `argocd.git` / `argocd.infra` repository block accepts.
_REPO_KEYS = {"url"}
#: Keys the secrets `argocd.git` credentials block accepts.
_GIT_CRED_KEYS = {"username", "token"}
#: Apps whose `version:` is forwarded to the rendered chart. Setting `version`
#: on any other app (`ingress`, `nfs`, `monitoring`) is silently ignored by the
#: renderer today, so it is refused here instead; Traefik's pin lives at
#: `argocd.ingress.traefik.version`.
_VERSION_FORWARDED = {"metallb", "sealedsecrets", "certmanager", "cinder"}


def validate_argocd(root: Path) -> None:
    """Refuse a malformed or contradictory `argocd:` configuration.

    Called by core in converge's validate phase, before any cluster mutation, so
    a broken plugin section stops the run while the cluster is still untouched.
    Catches:
      - a non-mapping `argocd:` or sub-section (malformed settings),
      - a top-level `argocd:` key the plugin does not understand (unsupported
        options),
      - an unsupported key inside a per-app section (a typo or a `version`
        override the renderer would silently ignore),
      - only one of `git.url` / `infra.url` set (missing paired repository URLs),
      - git credentials in secrets.yaml without `git.url` (credentials without a
        Git URL),
      - a `url`/`token` apply target without a `kubeconfig`/`context` (an
        unsupported mode the plugin would otherwise silently discard).
    Raises ConfigError on the first problem. A missing config file is treated as
    absent configuration (the plugin has nothing supplied to validate), matching
    how activation already tolerates a missing file; core enforces that the files
    exist for a real converge.
    """
    dc = _read_cluster(root)
    ds = _read_secrets(root)
    where = CLUSTER_FILE
    clan_raw = dc.get("argocd")
    sec_raw = ds.get("argocd")
    clan: dict[str, Any] = clan_raw if isinstance(clan_raw, dict) else {}
    sec: dict[str, Any] = sec_raw if isinstance(sec_raw, dict) else {}
    if clan_raw is not None and not isinstance(clan_raw, dict):
        raise ConfigError(f"{where}: argocd must be a YAML mapping")
    if sec_raw is not None and not isinstance(sec_raw, dict):
        raise ConfigError(f"{SECRETS_FILE}: argocd must be a YAML mapping")

    # a miscapped or unsupported key inside the `argocd:` secrets section is
    # refused rather than silently discarded by activation.
    unknown = sorted(set(sec) - _ARGOCD_SECRETS_KEYS)
    if unknown:
        raise ConfigError(
            f"{SECRETS_FILE} (argocd): unsupported option(s): {', '.join(unknown)}; "
            "the plugin does not use them"
        )

    # apply-target values feed the kubectl subprocess and the repository Secret,
    # so they must be plain strings (a list or number would corrupt the command).
    for key in ("kubeconfig", "context", "url", "token"):
        value = sec.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"{SECRETS_FILE} (argocd.{key}) must be a string"
            )
    # A supplied `url`/`token` pair names the ArgoCD API, but the plugin applies
    # via kubectl only, so without a kubeconfig/context it is an unsupported
    # apply target that would be silently discarded by activation. Refuse it here
    # so the unsupported mode is reported up front instead of the plugin quietly
    # staying inactive.
    if (sec.get("url") or sec.get("token")) and not (
        sec.get("kubeconfig") or sec.get("context")
    ):
        raise ConfigError(
            f"{SECRETS_FILE} (argocd): url/token is not a supported apply target; "
            "the plugin applies manifests via kubectl only, so set "
            "argocd.kubeconfig or argocd.context instead of a url/token pair"
        )
    sgit = sec.get("git")
    if sgit is not None and not isinstance(sgit, dict):
        raise ConfigError(f"{SECRETS_FILE} (argocd.git) must be a YAML mapping")
    if isinstance(sgit, dict):
        cred_unknown = sorted(set(sgit) - _GIT_CRED_KEYS)
        if cred_unknown:
            raise ConfigError(
                f"{SECRETS_FILE} (argocd.git): unsupported option(s): "
                f"{', '.join(cred_unknown)}; the plugin does not use them"
            )
        for key in ("username", "token"):
            if key in sgit and not isinstance(sgit[key], str):
                raise ConfigError(
                    f"{SECRETS_FILE} (argocd.git.{key}) must be a string"
                )

    unknown = sorted(set(clan) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"{where} (argocd): unsupported option(s): {', '.join(unknown)}; "
            "the plugin does not use them"
        )
    for key in sorted(_MAPPING_KEYS & set(clan)):
        if not isinstance(clan[key], dict):
            raise ConfigError(f"{where} (argocd.{key}) must be a YAML mapping")

    # each git/infra repository block accepts only `url`; a misspelled or
    # unsupported key inside one is refused rather than silently dropped.
    for repo in ("git", "infra"):
        section = clan.get(repo)
        if isinstance(section, dict):
            repo_unknown = sorted(set(section) - _REPO_KEYS)
            if repo_unknown:
                raise ConfigError(
                    f"{where} (argocd.{repo}): unsupported key(s): {', '.join(repo_unknown)}"
                )

    # member role lists must be lists of full email addresses. A bare string
    # would iterate character by character when the renderer flattens it.
    for role in ("admins", "users"):
        members = clan.get(role)
        if members is not None and (
            not isinstance(members, list)
            or any(not isinstance(m, str) or not m.strip() for m in members)
        ):
            raise ConfigError(
                f"{where} (argocd.{role}) must be a list of email addresses"
            )

    # the two toggles are YAML booleans; a quoted "false" is a nonempty string
    # and must be refused rather than treated as truthy when rendering.
    for key, _default in (("sync", False), ("automated", True)):
        value = clan.get(key)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"{where} (argocd.{key}) must be a boolean (true or false)"
            )

    # repository URLs are non-empty strings.
    for repo in ("git", "infra"):
        section = clan.get(repo)
        if isinstance(section, dict) and "url" in section:
            url = section["url"]
            if not isinstance(url, str) or not url.strip():
                raise ConfigError(
                    f"{where} (argocd.{repo}.url) must be a non-empty string"
                )

    # each per-app section: refuse a misspelled or unsupported key, refuse a
    # `version` override the renderer would silently ignore, and require a
    # forwarded version to be a non-empty string.
    for app, allowed in _PER_APP_KEYS.items():
        section = clan.get(app)
        if not isinstance(section, dict):
            continue
        unknown = sorted(set(section) - allowed)
        if unknown:
            raise ConfigError(
                f"{where} (argocd.{app}): unsupported key(s): {', '.join(unknown)}"
            )
        if "enabled" in section and not isinstance(section["enabled"], bool):
            raise ConfigError(
                f"{where} (argocd.{app}.enabled) must be a boolean (true or false)"
            )
        if "version" in section and app not in _VERSION_FORWARDED:
            raise ConfigError(
                f"{where} (argocd.{app}.version): the plugin does not forward a "
                "chart version for this app; set version under argocd.metallb, "
                "argocd.sealedsecrets, argocd.certmanager, argocd.cinder, or "
                "argocd.ingress.traefik, or remove the key"
            )
        if app in _VERSION_FORWARDED and "version" in section:
            v = section["version"]
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(
                    f"{where} (argocd.{app}.version) must be a non-empty string"
                )

    ingress = clan.get("ingress")
    if isinstance(ingress, dict):
        traefik = ingress.get("traefik")
        if traefik is not None:
            if not isinstance(traefik, dict):
                raise ConfigError(
                    f"{where} (argocd.ingress.traefik) must be a YAML mapping"
                )
            unknown = sorted(set(traefik) - {"version"})
            if unknown:
                raise ConfigError(
                    f"{where} (argocd.ingress.traefik): unsupported key(s): "
                    f"{', '.join(unknown)}"
                )
            if "version" in traefik and (
                not isinstance(traefik["version"], str) or not traefik["version"].strip()
            ):
                raise ConfigError(
                    f"{where} (argocd.ingress.traefik.version) must be a non-empty string"
                )

    nfs = clan.get("nfs")
    if isinstance(nfs, dict):
        servers = nfs.get("servers")
        if servers is not None:
            if not isinstance(servers, dict) or any(
                not isinstance(s, dict) for s in servers.values()
            ):
                raise ConfigError(
                    f"{where} (argocd.nfs.servers) must map server names to mappings"
                )

    git = clan.get("git") or {}
    infra = clan.get("infra") or {}
    git_url = git.get("url")
    infra_url = infra.get("url")
    if bool(git_url) != bool(infra_url):
        missing = "infra.url" if git_url else "git.url"
        raise ConfigError(
            f"{where} (argocd): {missing} must be set together with the other "
            "repository URL; the repository Secret, root Application and cluster "
            "Application are only rendered together"
        )

    sgit = sec.get("git")
    if not isinstance(sgit, dict):
        sgit = {}
    if not git_url and (sgit.get("username") or sgit.get("token")):
        raise ConfigError(
            f"{SECRETS_FILE} (argocd.git): git credentials are set but "
            f"argocd.git.url in {where} is not, so no repository Secret can be "
            "rendered; set argocd.git.url or remove the credentials"
        )

    s_openstack = ds.get("openstack")
    if isinstance(s_openstack, dict):
        for key in ("credential_id", "credential_secret"):
            if key in s_openstack and not isinstance(s_openstack[key], str):
                raise ConfigError(
                    f"{SECRETS_FILE} (openstack.{key}) must be a string"
                )


def argocd_configured(root: Path) -> bool:
    """True when secrets.yaml names a supported ArgoCD apply target.

    Only a kubectl mode (kubeconfig or context) activates the plugin. A
    `url`/`token` pair alone names an ArgoCD API endpoint, which the plugin does
    not speak; refusing to report configured here keeps the plugin from showing
    as active and then failing in every hook.
    """
    try:
        d = read_yaml(root / SECRETS_FILE)
    except ConfigError:
        return False
    argocd = _section(d, "argocd")
    if not argocd:
        return False
    return bool(argocd.get("kubeconfig") or argocd.get("context"))
