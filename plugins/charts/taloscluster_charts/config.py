"""Load + validate the `charts:` section of cluster.yaml.

cluster.yaml (committed) -- each entry is one helm chart or one set of
manifests. Values taloscluster already knows (the metallb ingress pool) come
from the plugin Context, never from here:

    charts:
      gateway:
        enabled: true
        version: latest          # newest gateway-api release; or a pinned tag
      metallb:
        enabled: true
        version: latest          # or a pinned chart version
        values: {}               # deep-merged over the plugin's common values
      traefik:
        enabled: true

Entry keys:

    enabled     bool, default true; false removes the release / manifests
    version     str; chart version; absent or "latest" upgrades only when a
                newer chart version exists upstream
    repo        str; helm chart repository, passed as `helm --repo`
    manifest    str | [str]; manifest url(s) applied with kubectl apply -f
    namespace   str | {name, enforce, audit, warn}; install namespace with the
                Pod-Security-Admission labels to set on it; chart entries only
                (an unknown entry defaults to one named after the entry)
    values      mapping; overrides deep-merged over the common values; chart
                entries only
    email       str; consumed by cert-manager (issuer account)
    staging     bool; cert-manager: adds a letsencrypt-staging ClusterIssuer
    prod        bool; cert-manager: adds a letsencrypt-prod ClusterIssuer
    userID      str; ceph: CephX user for the csi secrets (with userKey; optional,
    userKey     str; usually kept in secrets.yaml under the same charts.ceph path)

A known entry (cert-manager / gateway / metallb / sealed-secrets / traefik)
ships builtin defaults; any other entry must set `repo` or `manifest` itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from taloscluster.config import load_raw
from taloscluster.errors import ConfigError

SECTION = "charts"

ENTRY_KEYS = {
    "enabled", "version", "repo", "manifest", "namespace", "values", "email", "staging", "prod",
    "storageClasses", "clusterID", "monitors", "rbd", "fs", "userID", "userKey",
}

# keys a `namespace:` mapping may carry
NAMESPACE_KEYS = {"name", "enforce", "audit", "warn"}

# keys each item of a `storageClasses:` list may carry
STORAGE_CLASS_KEYS = {
    "name", "server", "share", "subDir", "onDelete", "defaultClass", "reclaimPolicy",
    "volumeBindingMode", "mountOptions", "annotations", "parameters",
}

# keys a ceph `rbd:` / `fs:` mapping (a chart-created StorageClass) may carry,
# plus the driver-specific `pool` (rbd, required) and `fsName` (fs, required)
CEPH_CLASS_KEYS = {
    "name", "defaultClass", "reclaimPolicy", "mountOptions", "annotations", "parameters",
}
CEPH_DRIVER_KEYS = {"rbd": {"pool"}, "fs": {"fsName", "pool"}}
CEPH_DRIVER_REQUIRED = {"rbd": "pool", "fs": "fsName"}

@dataclass(frozen=True)
class Namespace:
    """Install namespace and the PSA labels to set on it."""

    name: str
    enforce: str | None = None
    audit: str | None = None
    warn: str | None = None


@dataclass(frozen=True)
class Known:
    """Builtin defaults for a chart taloscluster knows by name."""

    kind: str                                  # "chart" (helm) or "manifest" (kubectl apply)
    chart: str | None = None                   # helm chart name (default: the entry name)
    repo: str | None = None                    # helm chart repo for kind "chart"
    manifest: tuple[str, ...] = ()             # manifest url template(s) for kind "manifest"
    namespace: Namespace | None = None
    email: bool = False                        # the entry consumes the `email` key
    issuers: bool = False                      # the entry consumes the staging/prod keys
    storage_classes: bool = False              # the entry consumes the storageClasses key
    ceph: bool = False              # the entry is the ceph one (clusterID/monitors/rbd/fs)


KNOWN: dict[str, Known] = {
    "ceph": Known(
        kind="chart",
        repo="https://ceph.github.io/csi-charts",
        ceph=True,
    ),
    "cert-manager": Known(
        kind="chart",
        repo="https://charts.jetstack.io",
        namespace=Namespace("cert-manager", "restricted", "restricted", "restricted"),
        email=True,
        issuers=True,
    ),
    "nfs": Known(
        kind="chart",
        chart="csi-driver-nfs",
        repo="https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts",
        namespace=Namespace("nfs", "privileged", "privileged", "privileged"),
        storage_classes=True,
    ),
    "gateway": Known(
        kind="manifest",
        manifest=(
            "https://github.com/kubernetes-sigs/gateway-api/releases/download/{version}"
            "/standard-install.yaml",
        ),
    ),
    "metallb": Known(
        kind="chart",
        repo="https://metallb.github.io/metallb",
        namespace=Namespace("metallb-system", "privileged", "privileged", "privileged"),
    ),
    "sealed-secrets": Known(
        kind="chart",
        repo="https://bitnami.github.io/sealed-secrets",
        namespace=Namespace("sealed-secrets", "restricted", "restricted", "restricted"),
    ),
    "traefik": Known(
        kind="chart",
        repo="https://traefik.github.io/charts",
        namespace=Namespace("traefik", "restricted", "restricted", "restricted"),
    ),
}


@dataclass(frozen=True)
class Entry:
    """One chart or manifest set under `charts:`."""

    name: str
    enabled: bool = True
    version: str = ""               # "" = latest
    chart: str | None = None        # helm chart to install (default: the entry name)
    repo: str | None = None         # helm chart repo; None for manifest entries
    manifest: tuple[str, ...] = ()  # manifest url template(s); empty for chart entries
    namespace: Namespace | None = None
    values: dict[str, Any] = field(default_factory=dict)
    email: str | None = None
    staging: bool = False           # cert-manager: letsencrypt-staging ClusterIssuer
    prod: bool = False              # cert-manager: letsencrypt-prod ClusterIssuer
    storage_classes: tuple[dict[str, Any], ...] = ()  # nfs: classes to create
    cluster_id: str = ""            # ceph: the cluster's fsid
    monitors: tuple[str, ...] = ()  # ceph: monitor addresses
    rbd: bool = False               # ceph: install the ceph-csi-rbd chart
    fs: bool = False                # ceph: install the ceph-csi-cephfs chart
    rbd_class: dict[str, Any] | None = None  # ceph: StorageClass the rbd chart creates
    fs_class: dict[str, Any] | None = None   # ceph: StorageClass the cephfs chart creates
    user_id: str = ""               # ceph: CephX user for the csi secrets (optional)
    user_key: str = ""              # ceph: CephX key for the csi secrets (optional)

    @property
    def is_chart(self) -> bool:
        return self.repo is not None

    @property
    def ceph_secrets(self) -> CephSecrets | None:
        """The CephX credentials for the csi secrets, when the entry carries them.

        Optional: a cluster can instead manage csi-rbd-secret / csi-cephfs-secret
        itself (sealed-secrets, out-of-band kubectl) -- converge only warns when
        they are absent here.
        """
        if self.user_id and self.user_key:
            return CephSecrets(user_id=self.user_id, user_key=self.user_key)
        return None

    @property
    def is_manifest(self) -> bool:
        return bool(self.manifest)

    @property
    def is_latest(self) -> bool:
        return self.version in ("", "latest")

    @property
    def chart_name(self) -> str:
        """The helm chart to install: the entry name unless a Known overrides it."""
        return self.chart or self.name

    def urls(self, resolved: str | None = None) -> tuple[str, ...]:
        """Manifest url(s) with `{version}` interpolated.

        `resolved` supplies the version for a `version: latest` entry -- the
        newest upstream tag, resolved by the caller (a network lookup).
        Entries with explicit urls have no template and ignore it.
        """
        version = self.version
        if version in ("", "latest"):
            version = resolved or ""
        if not version and any("{version}" in url for url in self.manifest):
            raise ConfigError(
                f"cluster.yaml: charts.{self.name}: cannot resolve the latest release; "
                "pin a version or retry"
            )
        return tuple(url.format(version=version or "") for url in self.manifest)


@dataclass(frozen=True)
class CephSecrets:
    """CephX credentials for the csi secrets, from `charts.ceph.userID`/`userKey`."""

    user_id: str
    user_key: str


@dataclass(frozen=True)
class Config:
    """The parsed `charts:` section, entry name -> Entry (cluster.yaml order)."""

    entries: dict[str, Entry]

    @classmethod
    def load(cls, root: Path) -> Config:
        """Parse cluster.yaml's `charts:` section, refusing a malformed one.

        An entirely absent section is valid but empty -- it is `configured()`
        that decides whether the plugin runs at all, while this only has to
        reject supplied-but-invalid configuration.
        """
        raw, _opted_in = load_raw(root)
        section = raw.get(SECTION)
        if section is None:
            return cls(entries={})
        if not isinstance(section, dict):
            raise ConfigError(f"cluster.yaml: {SECTION}: must be a mapping")
        entries = {name: _entry(name, raw) for name, raw in section.items()}
        _validate_combined(entries)
        return cls(entries=entries)


def charts_configured(root: Path) -> bool:
    """True when the merged configuration enables at least one `charts:` entry.

    A section with every entry disabled -- exactly what `taloscluster init`
    scaffolds -- does not activate the plugin: activation would demand helm on
    every cluster that never enabled a chart. A section that cannot be parsed
    is not configured either; the validate hook is what reports it, the way
    argocd and rancher do.
    """
    try:
        cfg = Config.load(root)
    except ConfigError:
        return False
    return any(entry.enabled for entry in cfg.entries.values())


def _validate_combined(entries: dict[str, Entry]) -> None:
    """Cross-entry rules.

    Two ACME clients cannot share the HTTP-01 challenge path: traefik's own
    resolver intercepts /.well-known/acme-challenge for every host its
    resolver carries and answers 404 for cert-manager's challenges, starving
    every certificate. The traefik entry therefore refuses an acme resolver
    (the old traefik.email-based setup) while cert-manager's issuers are on.
    """
    cert_manager = entries.get("cert-manager")
    traefik = entries.get("traefik")
    if not (
        cert_manager
        and cert_manager.enabled
        and (cert_manager.staging or cert_manager.prod)
        and traefik
        and traefik.enabled
    ):
        return
    resolver_in_values = bool(traefik.values.get("certificatesResolvers"))
    resolver_in_args = any(
        str(arg).lower().startswith("--certificatesresolvers")
        for arg in traefik.values.get("additionalArguments") or []
    )
    if resolver_in_values or resolver_in_args:
        raise ConfigError(
            "cluster.yaml: charts: cert-manager's letsencrypt issuers are enabled while "
            "traefik's values configure an acme resolver of their own "
            "(certificatesResolvers/--certificatesresolvers); the two ACME clients fight "
            "over the HTTP-01 challenge path. Drop the resolver from traefik's values and "
            "use the cert-manager issuers, or disable cert-manager (or its issuers)."
        )


def validate_charts(root: Path) -> None:
    """Refuse a malformed or contradictory `charts:` section (ConfigError)."""
    Config.load(root)


def _entry(name: str, raw: Any) -> Entry:
    where = f"cluster.yaml: {SECTION}.{name}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping")
    if unknown := sorted(set(raw) - ENTRY_KEYS):
        raise ConfigError(f"{where}: unsupported key(s): {', '.join(unknown)}")
    known = KNOWN.get(name)

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be a boolean")

    version = raw.get("version") or ""
    if not isinstance(version, str):
        raise ConfigError(f"{where}: version must be a string")

    repo = raw.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise ConfigError(f"{where}: repo must be a non-empty string")

    manifest = _manifest(name, raw.get("manifest"))
    if repo is not None and manifest:
        raise ConfigError(f"{where}: set either repo or manifest, not both")
    if known is None and repo is None and not manifest:
        raise ConfigError(f"{where}: unknown chart; set repo or manifest")
    if known is not None and known.kind == "manifest" and repo is not None:
        raise ConfigError(f"{where}: {name} is a manifest entry; it has no repo")
    if known is not None and known.kind == "chart" and manifest:
        raise ConfigError(f"{where}: {name} is a chart entry; set repo, not manifest")
    if manifest and version:
        raise ConfigError(f"{where}: version is not used by manifest entries")
    # manifests are applied as their documents stand: no helm values to merge
    # and no namespace to create
    manifest_entry = bool(manifest) or (known is not None and known.kind == "manifest")
    if manifest_entry and "values" in raw:
        raise ConfigError(f"{where}: values is not used by manifest entries")
    if manifest_entry and "namespace" in raw:
        raise ConfigError(f"{where}: namespace is not used by manifest entries")

    namespace = _namespace(name, raw.get("namespace"), known)
    if namespace is None and known is None and repo is not None:
        # an unknown chart entry installs into a namespace of its own, managed
        # like a configured one (created with the marker, removed on disable)
        namespace = Namespace(name=name)

    values = raw.get("values") or {}
    if not isinstance(values, dict):
        raise ConfigError(f"{where}: values must be a mapping")

    storage_classes = _storage_classes(name, raw.get("storageClasses"), known)
    if storage_classes and "storageClasses" in values:
        raise ConfigError(
            f"{where}: use storageClasses or values.storageClasses, not both"
        )
    if known is not None and known.storage_classes and enabled and not storage_classes:
        raise ConfigError(f"{where}: storageClasses is required when {name} is enabled")

    cluster_id = raw.get("clusterID") or ""
    monitors = raw.get("monitors") or ()
    rbd, rbd_class = raw.get("rbd", False), None
    fs, fs_class = raw.get("fs", False), None
    user_id = raw.get("userID")
    user_key = raw.get("userKey")
    if known is not None and known.ceph:
        if "namespace" in raw:
            raise ConfigError(
                f"{where}: namespace is not used by ceph; each chart gets its own"
            )
        rbd, rbd_class = _ceph_driver(name, "rbd", rbd)
        fs, fs_class = _ceph_driver(name, "fs", fs)
        if (rbd_class or fs_class) and "storageClass" in values:
            raise ConfigError(f"{where}: use rbd/fs mappings or values.storageClass, not both")
        if sum(1 for c in (rbd_class, fs_class) if c and c.get("defaultClass")) > 1:
            raise ConfigError(f"{where}: at most one of rbd and fs may set defaultClass: true")
        # the credentials are optional, but half a pair is a mistake
        if (user_id is None) != (user_key is None):
            raise ConfigError(f"{where}: set both userID and userKey, or neither")
        for key, value in (("userID", user_id), ("userKey", user_key)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"{where}: {key} must be a non-empty string")
        if enabled:
            if not isinstance(cluster_id, str) or not cluster_id.strip():
                raise ConfigError(f"{where}: clusterID is required when {name} is enabled")
            if not isinstance(monitors, list) or not monitors or not all(
                isinstance(m, str) and m.strip() for m in monitors
            ):
                raise ConfigError(f"{where}: monitors must be a non-empty list of addresses")
            if not (rbd or fs):
                raise ConfigError(f"{where}: enable at least one of rbd or fs")
    elif any(k in raw for k in ("clusterID", "monitors", "rbd", "fs", "userID", "userKey")):
        raise ConfigError(
            f"{where}: clusterID/monitors/rbd/fs/userID/userKey are only used by ceph"
        )
    monitors = tuple(monitors) if isinstance(monitors, list) else ()

    email = raw.get("email")
    if email is not None:
        if not isinstance(email, str) or not email.strip():
            raise ConfigError(f"{where}: email must be a non-empty string")
        if not (known and known.email):
            raise ConfigError(f"{where}: email is only used by cert-manager")

    staging = raw.get("staging", False)
    prod = raw.get("prod", False)
    for flag, value in (("staging", staging), ("prod", prod)):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: {flag} must be a boolean")
        if value and not (known and known.issuers):
            raise ConfigError(f"{where}: {flag} is only used by cert-manager")

    if known is not None and known.email and enabled and email is None:
        if staging or prod:
            raise ConfigError(f"{where}: email is required when the staging/prod issuers are on")

    if known is None:
        final_repo, final_manifest = repo, manifest
    elif known.kind == "chart":
        final_repo, final_manifest = repo or known.repo, ()
    else:
        final_repo, final_manifest = None, known.manifest

    return Entry(
        name=name,
        enabled=enabled,
        version=version,
        chart=known.chart if known else None,
        repo=final_repo,
        manifest=final_manifest,
        namespace=namespace,
        values=values,
        email=email,
        staging=staging,
        prod=prod,
        storage_classes=storage_classes,
        cluster_id=cluster_id,
        monitors=monitors,
        rbd=rbd,
        rbd_class=rbd_class,
        fs_class=fs_class,
        fs=fs,
        user_id=user_id or "",
        user_key=user_key or "",
    )


def _ceph_driver(name: str, key: str, raw: Any) -> tuple[bool, dict[str, Any] | None]:
    """Normalize a ceph `rbd:` / `fs:` value: a boolean, or a mapping that also
    has the chart create a StorageClass (pool for rbd, fsName for fs)."""
    where = f"cluster.yaml: {SECTION}.{name}"
    if isinstance(raw, bool):
        return raw, None
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: {key} must be a boolean or a mapping")
    if unknown := sorted(set(raw) - CEPH_CLASS_KEYS - CEPH_DRIVER_KEYS[key]):
        raise ConfigError(f"{where}: {key}: unsupported key(s): {', '.join(unknown)}")
    required = CEPH_DRIVER_REQUIRED[key]
    for field_name in (required, "name", "pool"):
        value = raw.get(field_name)
        if field_name == required or value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{where}: {key}.{field_name} must be a non-empty string")
    return True, raw


def _storage_classes(name: str, raw: Any, known: Known | None) -> tuple[dict[str, Any], ...]:
    """Validate the `storageClasses:` list (nfs) and normalize it.

    Each class becomes the chart's storageClasses item: parameters.server and
    parameters.share carry the export, subDir defaults to a per-cluster
    pattern, and at most one class may be the cluster default.
    """
    where = f"cluster.yaml: {SECTION}.{name}"
    if raw is None:
        return ()
    if not (known and known.storage_classes):
        raise ConfigError(f"{where}: storageClasses is only used by nfs")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where}: storageClasses must be a non-empty list")
    seen: set[str] = set()
    defaults = 0
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: storageClasses entries must be mappings")
        if unknown := sorted(set(item) - STORAGE_CLASS_KEYS):
            raise ConfigError(f"{where}: storageClasses: unsupported key(s): {', '.join(unknown)}")
        for key in ("name", "server", "share"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{where}: storageClasses.{key} must be a non-empty string")
        if item["name"] in seen:
            raise ConfigError(f"{where}: storageClasses: duplicate name {item['name']!r}")
        seen.add(item["name"])
        if item.get("defaultClass"):
            defaults += 1
        out.append(item)
    if defaults > 1:
        raise ConfigError(f"{where}: at most one storageClass may set defaultClass: true")
    return tuple(out)


def _manifest(name: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or not all(
        isinstance(u, str) and u.strip() for u in raw
    ):
        raise ConfigError(f"cluster.yaml: {SECTION}.{name}: manifest must be a url or url list")
    return tuple(raw)


def _namespace(name: str, raw: Any, known: Known | None) -> Namespace | None:
    where = f"cluster.yaml: {SECTION}.{name}"
    if raw is None:
        return known.namespace if known else None
    if isinstance(raw, str):
        if not raw.strip():
            raise ConfigError(f"{where}: namespace must not be empty")
        return Namespace(name=raw)
    if isinstance(raw, dict):
        if unknown := sorted(set(raw) - NAMESPACE_KEYS):
            raise ConfigError(f"{where}: namespace: unsupported key(s): {', '.join(unknown)}")
        ns_name = raw.get("name")
        if not isinstance(ns_name, str) or not ns_name.strip():
            raise ConfigError(f"{where}: namespace.name must be a non-empty string")
        labels = {}
        for key in ("enforce", "audit", "warn"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ConfigError(f"{where}: namespace.{key} must be a non-empty string")
                labels[key] = value
        return Namespace(name=ns_name, **labels)
    raise ConfigError(f"{where}: namespace must be a name or a name/labels mapping")


def merge_values(common: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge user values over the common ones: dicts merge, lists replace."""
    out = dict(common)
    for key, value in user.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = merge_values(out[key], value)
        else:
            out[key] = value
    return out


def _version_key(v: str) -> tuple:
    """Numeric-aware sort key for a version string ("v1.3.1" -> ((0,1),(0,3),(0,1)))."""
    parts = re.split(r"[.\-+_]", v.strip().lstrip("vV"))
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


def is_newer(candidate: str, installed: str) -> bool:
    """Best-effort version comparison: True when candidate > installed."""
    try:
        return _version_key(candidate) > _version_key(installed)
    except (TypeError, ValueError):  # incomparable shapes: assume no upgrade
        return False


def same_version(a: str | None, b: str | None) -> bool:
    """Version equality ignoring a leading `v` (charts tag v1.21.2, --version takes 1.21.2).

    A missing version (no release to compare) is never equal.
    """
    if a is None or b is None:
        return False
    return _no_v_prefix(a) == _no_v_prefix(b)


def _no_v_prefix(version: str) -> str:
    version = version.strip()
    return version[1:] if version[:1] in ("v", "V") and version[1:2].isdigit() else version
