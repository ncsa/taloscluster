"""Load + validate cluster.yaml and secrets.yaml into typed objects, and expand
the node pools into the flat `machines` map (keyed by hostname) that the rest of
the tool converges against.

This replaces two things at once: terraform's `yamldecode(cluster.yaml)` +
`local.machines`, and the shell script's `yq` reads. Parsing is native (PyYAML),
so yq/jq disappear.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .naming import BASE_EXTENSIONS

CLUSTER_FILE = "cluster.yaml"
SECRETS_FILE = "secrets.yaml"

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Machine:
    """One node, fully resolved (the heir of terraform's local.machines value)."""

    name: str
    role: str          # controlplane | worker
    pool: str          # controlplane | <worker pool name>
    flavor: str
    disk: int          # GB, boot volume
    extensions: tuple[str, ...]        # resolved: base + cluster + pool, sorted
    config_patches: tuple[str, ...]    # freeform YAML docs, cluster + pool
    tags: dict[str, str] = field(default_factory=dict)  # node labels, cluster + pool


@dataclass(frozen=True)
class Secrets:
    openstack_credential_id: str
    openstack_credential_secret: str
    tailscale_auth_key: str | None     # None => tailscale extension idles


@dataclass
class Config:
    name: str
    talos_version: str
    kubernetes_version: str
    # extensions/patches applied to every node, on top of BASE_EXTENSIONS
    talos_extensions: list[str]
    talos_config_patches: list[str]
    # extra tags exposed by talos as kubernetes node labels (machine.nodeLabels)
    tags: dict[str, str]

    controlplane: dict[str, Any]       # count / flavor / disk
    workers: dict[str, dict[str, Any]] # pool -> {count, flavor, disk, extensions?, config_patches?}

    openstack_url: str
    availability_zone: str
    external_net: str

    cidr: str
    dns: list[str]
    ntp: list[str]

    # allowlists: friendly name -> CIDR
    security_kubernetes: dict[str, str]
    security_talos: dict[str, str]

    login_server: str | None           # headscale/tailscale login server

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived ------------------------------------------------------------

    @cached_property
    def machines(self) -> dict[str, Machine]:
        """Flat hostname -> Machine map: controlplane pool + every worker pool.

        Keyed by hostname (like terraform's for_each) so adding/removing a node
        never renumbers the survivors.
        """
        out: dict[str, Machine] = {}

        cp = self.controlplane
        for i in range(1, _int(cp["count"], "count", "pool 'controlplane'") + 1):
            host = f"{self.name}-controlplane-{i:02d}"
            out[host] = Machine(
                name=host,
                role="controlplane",
                pool="controlplane",
                flavor=cp["flavor"],
                disk=_int(cp["disk"], "disk", "pool 'controlplane'"),
                extensions=self._resolve_extensions(cp),
                config_patches=self._resolve_patches(cp),
                tags=self._resolve_tags(cp),
            )

        for pool, p in self.workers.items():
            for i in range(1, _int(p["count"], "count", f"pool '{pool}'") + 1):
                host = f"{self.name}-{pool}-{i:02d}"
                out[host] = Machine(
                    name=host,
                    role="worker",
                    pool=pool,
                    flavor=p["flavor"],
                    disk=_int(p["disk"], "disk", f"pool '{pool}'"),
                    extensions=self._resolve_extensions(p),
                    config_patches=self._resolve_patches(p),
                    tags=self._resolve_tags(p),
                )
        return out

    def extension_sets(self) -> set[tuple[str, ...]]:
        """The distinct resolved extension sets in use -> one image per set."""
        return {m.extensions for m in self.machines.values()}

    def _resolve_extensions(self, pool: dict[str, Any]) -> tuple[str, ...]:
        merged = set(BASE_EXTENSIONS)
        merged.update(self.talos_extensions)
        merged.update(pool.get("extensions", []) or [])
        return tuple(sorted(merged))

    def _resolve_tags(self, pool: dict[str, Any]) -> dict[str, str]:
        # cluster-wide tags first, pool-specific tags override on key collision
        merged = {str(k): str(v) for k, v in self.tags.items()}
        merged.update({str(k): str(v) for k, v in (pool.get("tags", {}) or {}).items()})
        return merged

    def _resolve_patches(self, pool: dict[str, Any]) -> tuple[str, ...]:
        # cluster-wide freeform patches first, then pool-specific (pool wins as
        # it is applied later in the --config-patch stack)
        patches = list(self.talos_config_patches)
        patches.extend(pool.get("config_patches", []) or [])
        return tuple(patches)


# ---------------------------------------------------------------------------
# loading + validation
# ---------------------------------------------------------------------------

def read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping")
    return data


def require(d: dict[str, Any], *keys: str, where: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            raise ConfigError(f"{where}: missing '{'.'.join(keys)}'")
        cur = cur[k]
    return cur


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a YAML mapping")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ConfigError(f"{field} must be a list of non-empty strings")
    return value


def load_config(root: Path) -> Config:
    d = read_yaml(root / CLUSTER_FILE)
    where = CLUSTER_FILE

    talos = _mapping(d.get("talos"), f"{where}: talos")
    controlplane = _mapping(require(d, "controlplane", where=where),
                            f"{where}: controlplane")
    workers = _mapping(d.get("workers"), f"{where}: workers")
    tags = _mapping(d.get("tags"), f"{where}: tags")
    security = _mapping(d.get("security"), f"{where}: security")
    tailscale = _mapping(d.get("tailscale"), f"{where}: tailscale")
    cfg = Config(
        name=require(d, "name", where=where),
        talos_version=require(d, "talos", "version", where=where),
        kubernetes_version=require(d, "kubernetes", "version", where=where),
        talos_extensions=_string_list(talos.get("extensions"),
                                      f"{where}: talos.extensions"),
        talos_config_patches=_string_list(talos.get("config_patches"),
                                          f"{where}: talos.config_patches"),
        tags=tags,
        controlplane=controlplane,
        workers=workers,
        openstack_url=require(d, "openstack", "url", where=where),
        availability_zone=require(d, "openstack", "availability_zone", where=where),
        external_net=require(d, "openstack", "external_net", where=where),
        cidr=require(d, "network", "cidr", where=where),
        dns=_string_list(require(d, "network", "dns", where=where),
                         f"{where}: network.dns"),
        ntp=_string_list(require(d, "network", "ntp", where=where),
                         f"{where}: network.ntp"),
        security_kubernetes=_mapping(security.get("kubernetes"),
                                     f"{where}: security.kubernetes"),
        security_talos=_mapping(security.get("talos"), f"{where}: security.talos"),
        login_server=tailscale.get("login_server"),
        raw=d,
    )
    _validate(cfg)
    return cfg


def load_secrets(root: Path) -> Secrets:
    d = read_yaml(root / SECRETS_FILE)
    where = SECRETS_FILE
    ts = d.get("tailscale", {}) or {}
    return Secrets(
        openstack_credential_id=require(d, "openstack", "credential_id", where=where),
        openstack_credential_secret=require(d, "openstack", "credential_secret", where=where),
        tailscale_auth_key=ts.get("auth_key"),
    )


def _int(value: Any, field: str, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")


def _validate(cfg: Config) -> None:
    """Reject invalid or ambiguous desired state before touching the cluster."""
    if not isinstance(cfg.name, str) or not _NAME_RE.fullmatch(cfg.name):
        raise ConfigError(
            "cluster.yaml: name must contain lowercase letters, numbers and internal hyphens"
        )
    for field_name, version in (
        ("talos.version", cfg.talos_version),
        ("kubernetes.version", cfg.kubernetes_version),
    ):
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise ConfigError(
                f"cluster.yaml: {field_name} must be a release version such as v1.2.3"
            )

    if "controlplane" in cfg.workers:
        raise ConfigError("worker pool name 'controlplane' is reserved")

    pools = {"controlplane": cfg.controlplane, **cfg.workers}
    for pool_name, p in pools.items():
        if not isinstance(pool_name, str) or not _NAME_RE.fullmatch(pool_name):
            raise ConfigError(f"worker pool name {pool_name!r} is not a valid hostname component")
        if not isinstance(p, dict):
            raise ConfigError(f"pool '{pool_name}' must be a YAML mapping")
        for key in ("count", "flavor", "disk"):
            if key not in p:
                raise ConfigError(f"pool '{pool_name}' missing '{key}'")
        count = _int(p["count"], "count", f"pool '{pool_name}'")
        if pool_name == "controlplane" and count < 1:
            raise ConfigError("pool 'controlplane': 'count' must be at least 1")
        if pool_name != "controlplane" and count < 0:
            raise ConfigError(f"pool '{pool_name}': 'count' must be zero or greater")
        if not isinstance(p["flavor"], str) or not p["flavor"].strip():
            raise ConfigError(f"pool '{pool_name}': 'flavor' must be a non-empty string")
        if _int(p["disk"], "disk", f"pool '{pool_name}'") <= 0:
            raise ConfigError(f"pool '{pool_name}': 'disk' must be greater than zero")
        _string_list(p.get("extensions"), f"pool '{pool_name}': extensions")
        _string_list(p.get("config_patches"), f"pool '{pool_name}': config_patches")
        _mapping(p.get("tags"), f"pool '{pool_name}': tags")
        if len(f"{cfg.name}-{pool_name}-01") > 63:
            raise ConfigError("cluster and pool names make a hostname longer than 63 characters")

    for field_name, value in (
        ("openstack.url", cfg.openstack_url),
        ("openstack.availability_zone", cfg.availability_zone),
        ("openstack.external_net", cfg.external_net),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"cluster.yaml: {field_name} must be a non-empty string")
    if cfg.login_server is not None and not isinstance(cfg.login_server, str):
        raise ConfigError("cluster.yaml: tailscale.login_server must be a string")

    if not isinstance(cfg.cidr, str):
        raise ConfigError("cluster.yaml: network.cidr must be a CIDR string")
    try:
        network = ipaddress.ip_network(cfg.cidr, strict=True)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: network.cidr is not a valid network: {cfg.cidr!r}"
        ) from None
    if network.version != 4:
        raise ConfigError("cluster.yaml: network.cidr must be IPv4")
    for dns in cfg.dns:
        try:
            ipaddress.ip_address(dns)
        except ValueError:
            raise ConfigError(
                f"cluster.yaml: network.dns contains an invalid address: {dns!r}"
            ) from None
    for field_name, allowlist in (
        ("security.kubernetes", cfg.security_kubernetes),
        ("security.talos", cfg.security_talos),
    ):
        for label, cidr in allowlist.items():
            if not isinstance(label, str) or not label or not isinstance(cidr, str):
                raise ConfigError(
                    f"cluster.yaml: {field_name} must map names to CIDR strings"
                )
            try:
                allowed = ipaddress.ip_network(cidr, strict=True)
            except ValueError:
                raise ConfigError(
                    f"cluster.yaml: {field_name}.{label} has invalid CIDR {cidr!r}"
                ) from None
            if allowed.version != 4:
                raise ConfigError(f"cluster.yaml: {field_name}.{label} must be IPv4")


def validate_warnings(cfg: Config) -> list[str]:
    warnings: list[str] = []
    cp_count = _int(cfg.controlplane["count"], "count", "pool 'controlplane'")
    if cp_count % 2 == 0:
        warnings.append(f"even controlplane count ({cp_count}), etcd needs a majority")
    if cp_count == 1:
        warnings.append("single controlplane, no HA")
    return warnings
