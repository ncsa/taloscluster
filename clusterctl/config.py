"""Load + validate cluster.yaml and secrets.yaml into typed objects, and expand
the node pools into the flat `machines` map (keyed by hostname) that the rest of
the tool converges against.

This replaces two things at once: terraform's `yamldecode(cluster.yaml)` +
`local.machines`, and the shell script's `yq` reads. Parsing is native (PyYAML),
so yq/jq disappear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .naming import BASE_EXTENSIONS

CLUSTER_FILE = "cluster.yaml"
SECRETS_FILE = "secrets.yaml"


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

    def _resolve_patches(self, pool: dict[str, Any]) -> tuple[str, ...]:
        # cluster-wide freeform patches first, then pool-specific (pool wins as
        # it is applied later in the --config-patch stack)
        patches = list(self.talos_config_patches)
        patches.extend(pool.get("config_patches", []) or [])
        return tuple(patches)


# ---------------------------------------------------------------------------
# loading + validation
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping")
    return data


def _require(d: dict[str, Any], *keys: str, where: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            raise ConfigError(f"{where}: missing '{'.'.join(keys)}'")
        cur = cur[k]
    return cur


def load_config(root: Path) -> Config:
    d = _read_yaml(root / CLUSTER_FILE)
    where = CLUSTER_FILE

    talos = d.get("talos", {}) or {}
    cfg = Config(
        name=_require(d, "name", where=where),
        talos_version=_require(d, "talos", "version", where=where),
        kubernetes_version=_require(d, "kubernetes", "version", where=where),
        talos_extensions=list(talos.get("extensions", []) or []),
        talos_config_patches=list(talos.get("config_patches", []) or []),
        controlplane=_require(d, "controlplane", where=where),
        workers=d.get("workers", {}) or {},
        openstack_url=_require(d, "openstack", "url", where=where),
        availability_zone=_require(d, "openstack", "availability_zone", where=where),
        external_net=_require(d, "openstack", "external_net", where=where),
        cidr=_require(d, "network", "cidr", where=where),
        dns=list(_require(d, "network", "dns", where=where)),
        ntp=list(_require(d, "network", "ntp", where=where)),
        security_kubernetes=dict((d.get("security", {}) or {}).get("kubernetes", {}) or {}),
        security_talos=dict((d.get("security", {}) or {}).get("talos", {}) or {}),
        login_server=(d.get("tailscale", {}) or {}).get("login_server"),
        raw=d,
    )
    _validate(cfg)
    return cfg


def load_secrets(root: Path) -> Secrets:
    d = _read_yaml(root / SECRETS_FILE)
    where = SECRETS_FILE
    ts = d.get("tailscale", {}) or {}
    return Secrets(
        openstack_credential_id=_require(d, "openstack", "credential_id", where=where),
        openstack_credential_secret=_require(d, "openstack", "credential_secret", where=where),
        tailscale_auth_key=ts.get("auth_key"),
    )


def _int(value: Any, field: str, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}") from None


def _validate(cfg: Config) -> None:
    """Hard errors only; soft issues are reported by validate_warnings."""
    for pool_name, p in {"controlplane": cfg.controlplane, **cfg.workers}.items():
        for key in ("count", "flavor", "disk"):
            if key not in p:
                raise ConfigError(f"pool '{pool_name}' missing '{key}'")


def validate_warnings(cfg: Config) -> list[str]:
    warnings: list[str] = []
    cp_count = _int(cfg.controlplane["count"], "count", "pool 'controlplane'")
    if cp_count % 2 == 0:
        warnings.append(f"even controlplane count ({cp_count}), etcd needs a majority")
    if cp_count == 1:
        warnings.append("single controlplane, no HA")
    return warnings
