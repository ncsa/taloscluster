"""Load + validate the `argocd:` section of cluster.yaml and secrets.yaml.

cluster.yaml (committed) -- role members are full email addresses:
    argocd:
      admins: [alice@example.com]   # -> project 'admin' role
      users:  [carol@example.com]   # -> project 'user' role

These are merged with the `rancher:` members (if a rancher section exists), so the
AppProject ends up reflecting both the Rancher access and any extra ArgoCD-only
access.

secrets.yaml (gitignored) -- how to reach the ArgoCD cluster to apply changes.
Any one of: a kubeconfig path, a kubectl context (uses the default kubeconfig,
e.g. ~/.kube/config), or an ArgoCD URL + token:
    argocd:
      kubeconfig: ../some-argocd-kubeconfig
      # or --
      context: argocd                   # kubectl --context (default kubeconfig)
      # or --
      url:   https://argocd.example.com
      token: <argocd-token>
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
    - `url` / `token`: alternative ArgoCD API credentials (unused in kubectl mode).
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
    openstack: Openstack | None = None
    metallb: dict[str, Any] = field(default_factory=dict)
    ingress: dict[str, Any] = field(default_factory=dict)
    sealedsecrets: dict[str, Any] = field(default_factory=dict)
    certmanager: dict[str, Any] = field(default_factory=dict)
    cinder: dict[str, Any] = field(default_factory=dict)
    nfs: dict[str, Any] = field(default_factory=dict)
    sync: bool = False

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

        # project roles = rancher members (if present) merged with argocd members
        admins = _uniq(*clan.get("admins") or [], *rancher.get("admins") or [])
        users = _uniq(*clan.get("users") or [], *rancher.get("users") or [])
        return cls(
            name=name,
            members=Members(admins=admins, users=users),
            git_url=git.get("url"),
            openstack=_load_openstack(d),
            metallb=metallb,
            ingress=ingress,
            sealedsecrets=sealedsecrets,
            certmanager=certmanager,
            cinder=cinder,
            nfs=nfs,
            sync=bool(clan.get("sync")),
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


def _uniq(*values: str) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return tuple(out)


def argocd_configured(root: Path) -> bool:
    """True when secrets.yaml has an `argocd:` apply target.

    Sufficient if any one of kubeconfig / context / url+token is present.
    """
    try:
        d = read_yaml(root / SECRETS_FILE)
    except ConfigError:
        return False
    argocd = _section(d, "argocd")
    if not argocd:
        return False
    if argocd.get("kubeconfig") or argocd.get("context"):
        return True
    return bool(argocd.get("url") and argocd.get("token"))
