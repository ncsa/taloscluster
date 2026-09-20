"""Load + validate cluster.yaml and secrets.yaml into typed objects, and expand
the node pools into the flat `machines` map (keyed by hostname) that the rest of
the tool converges against.

Parsing is native (PyYAML) with no external preprocessing; validation happens
up front so later phases only ever see a consistent, resolved config.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from . import naming, versions
from .errors import ConfigError
from .naming import BASE_EXTENSIONS

CLUSTER_FILE = "cluster.yaml"
SECRETS_FILE = "secrets.yaml"

# Top-level `cluster.yaml` keys taloscluster understands itself. Plugin-owned
# sections (e.g. `argocd:`, `rancher:`) are added from each installed plugin's
# CONFIG_SECTIONS, and `openstack`/`proxmox` is the one selected provider.
_CLUSTER_KEYS = {
    "name", "tags", "talos", "kubernetes", "controlplane", "workers",
    "network", "security", "tailscale", "openstack", "proxmox",
}
# Top-level `secrets.yaml` keys taloscluster reads (plus plugin sections).
_SECRETS_KEYS = {"tailscale", "openstack", "proxmox"}

# Direct keys each fixed-schema section of `cluster.yaml` accepts. These catch
# a miscapped or unsupported key inside a section -- `talos.extensons`,
# `network.dnss`, `openstack.regoin` -- that the top-level allowlist alone would
# let load and be silently ignored. Freeform maps are deliberately not
# enumerated here: `tags`/pool `tags` and security host labels are label maps,
# and `config_patches` hold freeform YAML documents.
_TALOS_KEYS = {"version", "extensions", "config_patches"}
_NETWORK_KEYS = {"cidr", "dns", "ntp", "cluster", "external"}
#: Direct keys an L2 block (`network.cluster`, `network.external`) accepts.
_L2_KEYS = {"cidr", "gateway", "vlan", "mtu", "kubeapi_vip"}
#: L2 keys that only describe the externally routed network, never the node L2.
_L2_EXTERNAL_ONLY_KEYS = {"anchor_cidr", "ingress_pool"}
_KUBERNETES_KEYS = {"version"}
_TAILSCALE_KEYS = {"login_server"}
_PROVIDER_KEYS = {
    "openstack": {"url", "availability_zone", "external_net", "region"},
    "proxmox": {"url", "storage", "iso_storage", "cidata_storage",
                "placement_strategy", "nodes", "tls_verify", "network"},
}
#: Direct keys `proxmox.network` accepts. Both subsections are fixed-schema, so
#: a miscapped `clustr`/`extrnl` section is refused instead of ignored.
_PROXMOX_NETWORK_KEYS = {"cluster", "external"}
#: Direct keys `proxmox.network.cluster` accepts; exactly one of `bridge`,
#: `vnet` or `sdn` is required (see :func:`_validate`).
_PROXMOX_CLUSTER_KEYS = {"bridge", "vnet", "vlan", "kubeapi_vip", "sdn"}
#: Direct keys `proxmox.network.cluster.sdn` accepts.
_PROXMOX_SDN_KEYS = {"name", "zone", "controller", "asn", "vrf_tag", "tag",
                     "mtu", "nodes", "exit_nodes", "primary_exit_node"}
#: Direct keys `proxmox.network.external` accepts.
_PROXMOX_EXTERNAL_KEYS = {"bridge", "cidr", "gateway", "anchor_cidr",
                          "kubeapi_vip", "vlan", "ingress_pool"}
#: Keys a pool may carry; `tags` is a freeform label map and both `extensions` /
#: `config_patches` are freeform lists, so only the structural keys are fixed.
_POOL_KEYS = {"count", "flavor", "disk", "cores", "memory", "node",
              "extensions", "config_patches", "tags"}
#: Direct keys each `secrets.yaml` section accepts.
_SECRETS_SECTION_KEYS = {
    "openstack": {"credential_id", "credential_secret"},
    "proxmox": {"token_id", "token_secret"},
    "tailscale": {"auth_key"},
}

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# `taloscluster init` scaffolds this placeholder into secrets.yaml; leaving it
# in place must not survive loading, or the provider client fails deep in an
# opaque 401 instead of at configuration time.
SECRET_PLACEHOLDER = "CHANGE-ME"
# Oldest Talos release the generated machine configuration targets: the
# multi-document network kinds (LinkConfig, DHCPv4Config, Layer2VIPConfig,
# RoutingRuleConfig, ResolverConfig) all exist from v1.13.
MIN_TALOS_VERSION = "v1.13.0"
#: Ethernet MTU used for an L2 that does not set one.
DEFAULT_MTU = 1500
#: Smallest MTU a node may be configured with (the IPv6 minimum link MTU).
MIN_MTU = 1280
_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Machine:
    """One node, fully resolved from the pools and defaults."""

    name: str
    role: str          # controlplane | worker
    pool: str          # controlplane | <worker pool name>
    disk: int          # GB, boot volume
    extensions: tuple[str, ...]        # resolved: base + cluster + pool, sorted
    config_patches: tuple[str, ...]    # freeform YAML docs, cluster + pool
    flavor: str = ""                  # OpenStack flavor
    cores: int = 0                    # Proxmox virtual CPU count
    memory: int = 0                   # Proxmox memory in GB (cluster.yaml unit)
    node: str = ""                    # optional explicit Proxmox placement
    tags: dict[str, str] = field(default_factory=dict)  # node labels, cluster + pool


# Named security rules default to their well-known port; anything else must say.
DEFAULT_SECURITY_PORTS = {"kubernetes": 6443, "talos": 50000, "http": 80, "https": 443}
# Ports left wide open unless the matching named rule appears in `security:`.
OPEN_BY_DEFAULT = ("http", "https")


@dataclass(frozen=True)
class SecurityRule:
    """One named ingress allowance: a tcp port and the CIDRs allowed to reach it."""

    name: str
    port: int
    hosts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenStackConfig:
    url: str
    availability_zone: str
    external_net: str
    # Default region; override in cluster.yaml with `openstack.region`.
    region: str = "RegionOne"


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str
    storage: str = ""
    iso_storage: str = ""
    cidata_storage: str = "local"
    placement_strategy: str = "spread"
    nodes: tuple[str, ...] = ()
    tls_verify: bool | str = True
    network: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxmoxSdn:
    """Resolved managed-SDN settings (EVPN zone + VNet + subnet).

    Every cluster.yaml field is optional; this carries the derived defaults.
    Empty `exit_nodes` means every cluster node, offline included, resolved at
    reconcile time, and an empty `primary_exit_node` means the first resolved
    exit node.
    """

    name: str = ""  # the SDN zone/VNet id; defaults to the cluster name
    zone: str = "evpn"
    controller: str = "evpnctl"
    asn: int = 65000
    vrf_tag: int = 0
    tag: int = 0
    exit_nodes: tuple[str, ...] = ()
    primary_exit_node: str = ""
    mtu: int | None = None
    nodes: tuple[str, ...] = ()


def proxmox_sdn(cluster: str, provider: ProxmoxConfig) -> ProxmoxSdn | None:
    """The managed-SDN settings with defaults applied, or None when not opted in."""
    cluster_network = provider.network.get("cluster")
    if not isinstance(cluster_network, dict) or "sdn" not in cluster_network:
        return None
    raw = cluster_network.get("sdn") or {}
    vrf_tag = int(raw["vrf_tag"]) if raw.get("vrf_tag") is not None else naming.sdn_vni(cluster)
    tag = int(raw["tag"]) if raw.get("tag") is not None else vrf_tag + 1
    nodes = tuple(str(node) for node in raw.get("nodes") or ())
    exit_nodes = tuple(
        str(node) for node in raw.get("exit_nodes") or nodes or provider.nodes
    )
    primary = str(
        raw.get("primary_exit_node") or (exit_nodes[0] if exit_nodes else "")
    )
    return ProxmoxSdn(
        name=str(raw.get("name") or cluster),
        zone=str(raw.get("zone") or "evpn"),
        controller=str(raw.get("controller") or "evpnctl"),
        asn=int(raw["asn"]) if raw.get("asn") is not None else 65000,
        vrf_tag=vrf_tag,
        tag=tag,
        exit_nodes=exit_nodes,
        primary_exit_node=primary,
        mtu=int(raw["mtu"]) if raw.get("mtu") is not None else None,
        nodes=nodes,
    )


ProviderConfig = OpenStackConfig | ProxmoxConfig


@dataclass(frozen=True)
class OpenStackSecrets:
    credential_id: str
    credential_secret: str


@dataclass(frozen=True)
class ProxmoxSecrets:
    token_id: str
    token_secret: str


ProviderSecrets = OpenStackSecrets | ProxmoxSecrets


@dataclass(frozen=True, init=False)
class Secrets:
    provider: ProviderSecrets
    tailscale_auth_key: str | None     # None => tailscale extension idles

    def __init__(
        self,
        provider: ProviderSecrets | None = None,
        tailscale_auth_key: str | None = None,
        *,
        openstack_credential_id: str | None = None,
        openstack_credential_secret: str | None = None,
    ) -> None:
        """Keep the pre-provider constructor available to in-tree plugins/tests."""
        if provider is None:
            if openstack_credential_id is None or openstack_credential_secret is None:
                raise ConfigError("provider credentials are required")
            provider = OpenStackSecrets(
                credential_id=openstack_credential_id,
                credential_secret=openstack_credential_secret,
            )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "tailscale_auth_key", tailscale_auth_key)

    @property
    def openstack_credential_id(self) -> str:
        if not isinstance(self.provider, OpenStackSecrets):
            raise ConfigError("OpenStack credentials requested for a Proxmox cluster")
        return self.provider.credential_id

    @property
    def openstack_credential_secret(self) -> str:
        if not isinstance(self.provider, OpenStackSecrets):
            raise ConfigError("OpenStack credentials requested for a Proxmox cluster")
        return self.provider.credential_secret


@dataclass(frozen=True)
class L2Network:
    """One layer-2 network the nodes sit on, described the same way everywhere.

    `network.cluster` carries the node L2 of the VM provider; `network.external`
    the externally routed L2. `anchor_cidr` and `ingress_pool` only ever
    describe the external one.
    """

    cidr: str = ""
    gateway: str = ""
    vlan: int | None = None
    mtu: int = DEFAULT_MTU
    kubeapi_vip: str = ""
    anchor_cidr: str = ""     # external only
    ingress_pool: str = ""    # external only


def l2_facts(l2: L2Network) -> dict[str, Any]:
    """The facts an L2 sets, as the mapping the provider code reads them from.

    `mtu` is deliberately absent: these mappings feed the Proxmox plumbing,
    which has no MTU key, and the machine configuration reads the MTU from the
    :class:`L2Network` itself.
    """
    facts: dict[str, Any] = {
        key: getattr(l2, key)
        for key in ("cidr", "gateway", "kubeapi_vip", "anchor_cidr", "ingress_pool")
        if getattr(l2, key)
    }
    if l2.vlan is not None:
        facts["vlan"] = l2.vlan
    return facts


@dataclass(frozen=True)
class NetworkConfig:
    """The `network:` section: cluster-wide resolvers plus the L2 blocks."""

    dns: list[str]
    ntp: list[str]
    cluster: L2Network
    external: L2Network | None = None


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

    controlplane: dict[str, Any]       # count / provider sizing / disk
    workers: dict[str, dict[str, Any]] # pool -> count / provider sizing / disk / overrides

    provider: ProviderConfig

    network: NetworkConfig

    # named ingress allowlists, in cluster.yaml order
    security: dict[str, SecurityRule]

    login_server: str | None           # headscale/tailscale login server
    # a `tailscale:` section in cluster.yaml opts the installed system into the
    # tailscale extension; the shared boot ISO always carries it either way
    tailscale_enabled: bool = True

    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived ------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "openstack" if isinstance(self.provider, OpenStackConfig) else "proxmox"

    @property
    def openstack_url(self) -> str:
        return self._openstack.url

    @property
    def availability_zone(self) -> str:
        return self._openstack.availability_zone

    @property
    def external_net(self) -> str:
        return self._openstack.external_net

    @property
    def region(self) -> str:
        return self._openstack.region

    @property
    def security_kubernetes(self) -> dict[str, str]:
        return self.security_hosts("kubernetes")

    @property
    def security_talos(self) -> dict[str, str]:
        return self.security_hosts("talos")

    def security_hosts(self, name: str) -> dict[str, str]:
        rule = self.security.get(name)
        return dict(rule.hosts) if rule else {}

    def open_ports(self) -> tuple[int, ...]:
        """Ports left open to every source because no rule claims that port.

        Keyed on the resolved port, not the rule name: `http: {port: 8080}`
        restricts 8080 and leaves 80 open, which is what the ports say.
        """
        claimed = {rule.port for rule in self.security.values()}
        return tuple(
            port
            for port in (DEFAULT_SECURITY_PORTS[name] for name in OPEN_BY_DEFAULT)
            if port not in claimed
        )

    @property
    def _openstack(self) -> OpenStackConfig:
        if not isinstance(self.provider, OpenStackConfig):
            raise ConfigError("OpenStack configuration requested for a Proxmox cluster")
        return self.provider

    @cached_property
    def machines(self) -> dict[str, Machine]:
        """Flat hostname -> Machine map: controlplane pool + every worker pool.

        Keyed by hostname so adding/removing a node never renumbers the
        survivors.
        """
        out: dict[str, Machine] = {}

        cp = self.controlplane
        for i in range(1, _int(cp["count"], "count", "pool 'controlplane'") + 1):
            host = f"{self.name}-controlplane-{i:02d}"
            out[host] = Machine(
                name=host,
                role="controlplane",
                pool="controlplane",
                disk=_int(cp["disk"], "disk", "pool 'controlplane'"),
                extensions=self._resolve_extensions(cp),
                config_patches=self._resolve_patches(cp),
                flavor=str(cp.get("flavor") or ""),
                cores=_int(cp.get("cores", 0), "cores", "pool 'controlplane'"),
                memory=_int(cp.get("memory", 0), "memory", "pool 'controlplane'"),
                node=str(cp.get("node") or ""),
                tags=self._resolve_tags(cp),
            )

        for pool, p in self.workers.items():
            for i in range(1, _int(p["count"], "count", f"pool '{pool}'") + 1):
                host = f"{self.name}-{pool}-{i:02d}"
                out[host] = Machine(
                    name=host,
                    role="worker",
                    pool=pool,
                    disk=_int(p["disk"], "disk", f"pool '{pool}'"),
                    extensions=self._resolve_extensions(p),
                    config_patches=self._resolve_patches(p),
                    flavor=str(p.get("flavor") or ""),
                    cores=_int(p.get("cores", 0), "cores", f"pool '{pool}'"),
                    memory=_int(p.get("memory", 0), "memory", f"pool '{pool}'"),
                    node=str(p.get("node") or ""),
                    tags=self._resolve_tags(p),
                )
        return out

    def extension_sets(self) -> set[tuple[str, ...]]:
        """The distinct resolved extension sets in use -> one image per set."""
        return {m.extensions for m in self.machines.values()}

    def _resolve_extensions(self, pool: dict[str, Any]) -> tuple[str, ...]:
        # the boot ISO always bakes BASE_EXTENSIONS, but the installer image
        # (machine.install.image) drops tailscale when no tailscale: section is
        # configured, so the installed system does not carry a dormant service
        merged = set(BASE_EXTENSIONS)
        if not self.tailscale_enabled:
            merged.discard("siderolabs/tailscale")
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


def _plugin_config_sections() -> set[str]:
    """Top-level keys owned by installed plugins (e.g. a plugin's `argocd:`).

    Imported lazily because plugins import this module (plugins -> context ->
    config forms an import cycle). A plugin that does not declare its sections
    contributes nothing, so a bare core install accepts only the core keys below.
    """
    from . import plugins  # deferred to break plugins -> context -> config

    sections: set[str] = set()
    for plugin in plugins.discover():
        for name in getattr(plugin.module, "CONFIG_SECTIONS", ()) or ():
            sections.add(name)
    return sections


def _reject_unknown_keys(data: dict[str, Any], where: str, known: set[str]) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s): {', '.join(unknown)}; "
            "taloscluster does not use them"
        )


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


def _secret(name: str, value: Any, where: str) -> str:
    """Validate a secrets.yaml credential is a real, non-empty string.

    A null, non-string, empty, or scaffolded ``CHANGE-ME`` value is refused here
    because it would otherwise load cleanly and then fail deep in the provider
    client as an opaque 401.
    """
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {name} must be a non-empty string")
    if value == SECRET_PLACEHOLDER:
        raise ConfigError(
            f"{where}: {name} is still the scaffolded {SECRET_PLACEHOLDER!r} "
            "placeholder; set it to your real credential"
        )
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ConfigError(f"{field} must be a list of non-empty strings")
    return value


def _security_rules(security: dict[str, Any], where: str) -> dict[str, SecurityRule]:
    """Parse `security:` into named rules, in file order.

    Two shapes are accepted per entry. The named shape carries `hosts` (and an
    optional `port`); the legacy shape is a bare name-to-CIDR mapping, which is
    what every pre-0.5 cluster.yaml uses for `kubernetes` and `talos`. A `hosts`
    key only selects the named shape when it holds a mapping, so a legacy
    allowlist whose friendly name happens to be `hosts` still parses as a CIDR.
    """
    rules: dict[str, SecurityRule] = {}
    for name, value in security.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: security rule names must be non-empty strings")
        entry = _mapping(value, f"{where}: security.{name}")
        if isinstance(entry.get("hosts"), dict) or "port" in entry:
            hosts = _mapping(entry.get("hosts"), f"{where}: security.{name}.hosts")
            port = entry.get("port", DEFAULT_SECURITY_PORTS.get(name))
            unknown = sorted(set(entry) - {"hosts", "port"})
            if unknown:
                raise ConfigError(
                    f"{where}: security.{name} has unknown keys: {', '.join(unknown)}"
                )
        else:
            hosts = entry
            port = DEFAULT_SECURITY_PORTS.get(name)
        if port is None:
            raise ConfigError(
                f"{where}: security.{name} requires an explicit 'port' "
                "(only kubernetes, talos, http and https have defaults)"
            )
        port = _int(port, "port", f"{where}: security.{name}")
        if not 1 <= port <= 65535:
            raise ConfigError(f"{where}: security.{name}.port must be 1-65535")
        # `http` and `https` name a port as well as an allowlist: they are what
        # opens or closes 80 and 443. Letting them carry some other port makes
        # the name lie about which port the rule governs, so a different port
        # needs a differently named rule.
        if name in OPEN_BY_DEFAULT and port != DEFAULT_SECURITY_PORTS[name]:
            raise ConfigError(
                f"{where}: security.{name} cannot change its port from "
                f"{DEFAULT_SECURITY_PORTS[name]} to {port}; "
                f"use a differently named rule for port {port}"
            )
        rules[name] = SecurityRule(
            name=name,
            port=port,
            hosts={str(label): host for label, host in hosts.items()},
        )
    return rules


def _provider_config(d: dict[str, Any], where: str) -> ProviderConfig:
    selected = [name for name in ("openstack", "proxmox") if name in d]
    if len(selected) != 1:
        raise ConfigError(
            f"{where}: exactly one provider section is required: openstack or proxmox"
        )

    name = selected[0]
    provider = _mapping(d[name], f"{where}: {name}")
    _reject_unknown_keys(provider, f"{where}: {name}", _PROVIDER_KEYS[name])
    if name == "openstack":
        return OpenStackConfig(
            url=require(provider, "url", where=f"{where}: openstack"),
            availability_zone=require(
                provider, "availability_zone", where=f"{where}: openstack"
            ),
            external_net=require(provider, "external_net", where=f"{where}: openstack"),
            region=str(provider.get("region") or "RegionOne"),
        )
    return ProxmoxConfig(
        url=require(provider, "url", where=f"{where}: proxmox"),
        storage=str(provider.get("storage") or ""),
        iso_storage=str(provider.get("iso_storage") or ""),
        cidata_storage=str(provider.get("cidata_storage") or "local"),
        placement_strategy=str(provider.get("placement_strategy") or "spread"),
        nodes=tuple(_string_list(provider.get("nodes"), f"{where}: proxmox.nodes")),
        tls_verify=provider.get("tls_verify", True),
        network=_mapping(provider.get("network"), f"{where}: proxmox.network"),
    )


def _ipv4_network(value: Any, where: str, *, strict: bool = True) -> ipaddress.IPv4Network:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"cluster.yaml: {where} must be an IPv4 CIDR")
    try:
        net = ipaddress.ip_network(value, strict=strict)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: {where} is not a valid network: {value!r}"
        ) from None
    if not isinstance(net, ipaddress.IPv4Network):
        raise ConfigError(f"cluster.yaml: {where} must be IPv4")
    return net


def _ipv4_address(value: Any, where: str) -> ipaddress.IPv4Address:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"cluster.yaml: {where} must be an IPv4 address")
    try:
        addr = ipaddress.ip_address(value.strip())
    except (TypeError, ValueError):
        raise ConfigError(f"cluster.yaml: {where} is invalid: {value!r}") from None
    if not isinstance(addr, ipaddress.IPv4Address):
        raise ConfigError(f"cluster.yaml: {where} must be IPv4")
    return addr


def _ip_range(value: Any, where: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    """Parse a `start-end` IPv4 range such as an `ingress_pool`."""
    if not isinstance(value, str) or value.count("-") != 1:
        raise ConfigError(f"cluster.yaml: {where} must be 'start-end', got {value!r}")
    first, last = (part.strip() for part in value.split("-"))
    start = _ipv4_address(first, f"{where} start")
    end = _ipv4_address(last, f"{where} end")
    if int(start) > int(end):
        raise ConfigError(f"cluster.yaml: {where} start must be <= end")
    return start, end


def _parse_ipv4_network(value: Any) -> ipaddress.IPv4Network | None:
    """The network `value` describes, or None when it is not one (unreported)."""
    try:
        net = ipaddress.ip_network(value, strict=False)
    except (TypeError, ValueError):
        return None
    return net if isinstance(net, ipaddress.IPv4Network) else None


def _parse_ipv4_address(value: Any) -> ipaddress.IPv4Address | None:
    """The address `value` describes, or None when it is not one (unreported)."""
    try:
        addr = ipaddress.ip_address(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, AttributeError):
        return None
    return addr if isinstance(addr, ipaddress.IPv4Address) else None


def _parse_ip_range(value: Any) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None:
    """The `start-end` range `value` describes, or None (unreported)."""
    if not isinstance(value, str) or value.count("-") != 1:
        return None
    start = _parse_ipv4_address(value.split("-")[0])
    end = _parse_ipv4_address(value.split("-")[1])
    if start is None or end is None or start > end:
        return None
    return start, end


def _validate_l2(
    new: dict[str, Any], merged: dict[str, Any], where: str, *, external: bool
) -> None:
    """Validate the fields a `network.cluster`/`network.external` block sets.

    Only the keys given in the new block are checked; the same fact taken from
    an old location keeps being reported by the provider validators. The
    ingress pool and the VIP are checked against each other on the merged
    values, so the pair is caught whichever location supplied each half.
    """
    block: ipaddress.IPv4Network | None = None
    if "cidr" in new:
        block = _ipv4_network(new["cidr"], f"{where}.cidr", strict=not external)
    elif merged.get("cidr") is not None:
        block = _parse_ipv4_network(merged["cidr"])  # invalid: the old key reports it
    if "gateway" in new:
        gateway = _ipv4_address(new["gateway"], f"{where}.gateway")
        if block is not None and gateway not in block:
            raise ConfigError(f"cluster.yaml: {where}.gateway must be inside {where}.cidr")
    if "vlan" in new and not 1 <= _int(new["vlan"], "vlan", f"cluster.yaml: {where}") <= 4094:
        raise ConfigError(f"cluster.yaml: {where}.vlan must be 1-4094")
    if "mtu" in new and _int(new["mtu"], "mtu", f"cluster.yaml: {where}") < MIN_MTU:
        raise ConfigError(f"cluster.yaml: {where}.mtu must be {MIN_MTU} or greater")
    if "kubeapi_vip" in new:
        vip = _ipv4_address(new["kubeapi_vip"], f"{where}.kubeapi_vip")
        if block is not None and vip not in block:
            raise ConfigError(
                f"cluster.yaml: {where}.kubeapi_vip must be inside {where}.cidr"
            )
    if "anchor_cidr" in new:
        anchor = _ipv4_network(new["anchor_cidr"], f"{where}.anchor_cidr", strict=False)
        if not anchor.subnet_of(ipaddress.IPv4Network("169.254.0.0/16")):
            raise ConfigError(
                f"cluster.yaml: {where}.anchor_cidr must be inside 169.254.0.0/16"
            )
    if "ingress_pool" in new:
        start, end = _ip_range(new["ingress_pool"], f"{where}.ingress_pool")
        if block is not None and (start not in block or end not in block):
            raise ConfigError(
                f"cluster.yaml: {where}.ingress_pool must be inside {where}.cidr"
            )
    pool = _parse_ip_range(merged.get("ingress_pool"))
    vip_address = _parse_ipv4_address(merged.get("kubeapi_vip"))
    if pool is not None and vip_address is not None and pool[0] <= vip_address <= pool[1]:
        raise ConfigError(
            f"cluster.yaml: {where}.kubeapi_vip must not be inside ingress_pool "
            "(MetalLB could hand the API address to a service)"
        )


def _l2_network(
    new: dict[str, Any],
    old: dict[str, Any],
    old_paths: dict[str, str],
    where: str,
    *,
    external: bool,
) -> L2Network:
    """One L2 block, merged from the new `network.*` keys and the old locations."""
    keys = _L2_KEYS | _L2_EXTERNAL_ONLY_KEYS if external else _L2_KEYS
    merged: dict[str, Any] = {}
    for key in sorted(keys):
        fresh, legacy = new.get(key), old.get(key)
        # compare rendered values so `vlan: 21` and `vlan: "21"` are one fact
        if fresh is not None and legacy is not None and str(fresh) != str(legacy):
            raise ConfigError(
                f"cluster.yaml: {where}.{key} ({fresh!r}) conflicts with "
                f"{old_paths[key]} ({legacy!r}); set it in one place"
            )
        value = fresh if fresh is not None else legacy
        if value is not None:
            merged[key] = value
    _validate_l2(new, merged, where, external=external)
    return L2Network(
        cidr=str(merged.get("cidr", "")),
        gateway=str(merged.get("gateway", "")),
        vlan=_int(merged["vlan"], "vlan", f"cluster.yaml: {where}")
        if merged.get("vlan") is not None
        else None,
        mtu=_int(merged["mtu"], "mtu", f"cluster.yaml: {where}")
        if merged.get("mtu") is not None
        else DEFAULT_MTU,
        kubeapi_vip=str(merged.get("kubeapi_vip", "")),
        anchor_cidr=str(merged.get("anchor_cidr", "")),
        ingress_pool=str(merged.get("ingress_pool", "")),
    )


def _network_config(d: dict[str, Any], where: str) -> NetworkConfig:
    """Parse `network:` into the cluster-wide settings and the two L2 blocks.

    The old locations of the same facts (`network.cidr` and, on Proxmox,
    `proxmox.network.cluster.{vlan,kubeapi_vip}` and
    `proxmox.network.external.*`) still load; a fact set in both places must
    agree.
    """
    net = _mapping(d.get("network"), f"{where}: network")
    _reject_unknown_keys(net, f"{where}: network", _NETWORK_KEYS)

    cluster_raw = _mapping(net.get("cluster"), f"{where}: network.cluster")
    misplaced = sorted(_L2_EXTERNAL_ONLY_KEYS & set(cluster_raw))
    if misplaced:
        raise ConfigError(
            f"{where}: network.cluster: {', '.join(misplaced)} describe the externally "
            "routed network; set them under network.external"
        )
    _reject_unknown_keys(cluster_raw, f"{where}: network.cluster", _L2_KEYS)
    external_raw = _mapping(net.get("external"), f"{where}: network.external")
    _reject_unknown_keys(
        external_raw, f"{where}: network.external", _L2_KEYS | _L2_EXTERNAL_ONLY_KEYS
    )
    # an explicitly null key is the same as an absent one, as everywhere else
    cluster_raw = {k: v for k, v in cluster_raw.items() if v is not None}
    external_raw = {k: v for k, v in external_raw.items() if v is not None}

    proxmox = d.get("proxmox") if isinstance(d.get("proxmox"), dict) else {}
    provider_net = _mapping(proxmox.get("network"), f"{where}: proxmox.network")
    old_cluster_raw = _mapping(
        provider_net.get("cluster"), f"{where}: proxmox.network.cluster"
    )
    old_external_raw = _mapping(
        provider_net.get("external"), f"{where}: proxmox.network.external"
    )
    # the same rejections `_validate` makes, up front: a misspelled old key must
    # be reported as such, not as the fact it fails to supply
    _reject_unknown_keys(
        old_cluster_raw, f"{where}: proxmox.network.cluster", _PROXMOX_CLUSTER_KEYS
    )
    _reject_unknown_keys(
        old_external_raw, f"{where}: proxmox.network.external", _PROXMOX_EXTERNAL_KEYS
    )

    old_cluster = {k: v for k, v in old_cluster_raw.items() if k in _L2_KEYS}
    old_cluster_paths = {k: f"proxmox.network.cluster.{k}" for k in old_cluster}
    if net.get("cidr") is not None:
        old_cluster["cidr"] = net["cidr"]
        old_cluster_paths["cidr"] = "network.cidr"
    old_external = {
        k: v
        for k, v in old_external_raw.items()
        if k in _L2_KEYS | _L2_EXTERNAL_ONLY_KEYS
    }
    old_external_paths = {k: f"proxmox.network.external.{k}" for k in old_external}

    cluster = _l2_network(
        cluster_raw, old_cluster, old_cluster_paths, "network.cluster", external=False
    )
    if not cluster.cidr:
        raise ConfigError(f"{where}: missing 'network.cidr' (or 'network.cluster.cidr')")
    # any external section at all -- including a bare Proxmox `bridge` -- asks
    # for the externally routed network, which is only usable fully described
    external = (
        _l2_network(
            external_raw, old_external, old_external_paths, "network.external",
            external=True,
        )
        if external_raw or old_external_raw
        else None
    )
    if external is not None:
        for key, label in (
            ("cidr", "an IPv4 CIDR"),
            ("gateway", "an IPv4 address"),
            ("anchor_cidr", "an IPv4 CIDR"),
        ):
            if not getattr(external, key):
                raise ConfigError(
                    f"{where}: network.external.{key} must be {label}"
                )
        external_net = _parse_ipv4_network(external.cidr)
        cluster_net = _parse_ipv4_network(cluster.cidr)
        if (
            external_net is not None
            and cluster_net is not None
            and external_net.overlaps(cluster_net)
        ):
            raise ConfigError(
                f"{where}: network.external.cidr must not overlap network.cidr"
            )
    if cluster.kubeapi_vip and external is not None and external.kubeapi_vip:
        raise ConfigError(
            "kubeapi_vip must be set in only one of network.cluster or network.external"
        )
    return NetworkConfig(
        dns=_string_list(require(d, "network", "dns", where=where), f"{where}: network.dns"),
        ntp=_string_list(require(d, "network", "ntp", where=where), f"{where}: network.ntp"),
        cluster=cluster,
        external=external,
    )


def load_config(root: Path) -> Config:
    d = read_yaml(root / CLUSTER_FILE)
    where = CLUSTER_FILE
    _reject_unknown_keys(d, where, _CLUSTER_KEYS | _plugin_config_sections())

    talos = _mapping(d.get("talos"), f"{where}: talos")
    _reject_unknown_keys(talos, f"{where}: talos", _TALOS_KEYS)
    controlplane = _mapping(require(d, "controlplane", where=where),
                            f"{where}: controlplane")
    workers = _mapping(d.get("workers"), f"{where}: workers")
    _reject_unknown_keys(controlplane, f"{where}: controlplane", _POOL_KEYS)
    for pool_name, p in workers.items():
        if isinstance(p, dict):
            _reject_unknown_keys(p, f"{where}: workers.{pool_name}", _POOL_KEYS)
    tags = _mapping(d.get("tags"), f"{where}: tags")
    security = _mapping(d.get("security"), f"{where}: security")
    tailscale = _mapping(d.get("tailscale"), f"{where}: tailscale")
    _reject_unknown_keys(tailscale, f"{where}: tailscale", _TAILSCALE_KEYS)
    network = _network_config(d, where)
    _reject_unknown_keys(_mapping(d.get("kubernetes"), f"{where}: kubernetes"),
                         f"{where}: kubernetes", _KUBERNETES_KEYS)
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
        provider=_provider_config(d, where),
        network=network,
        security=_security_rules(security, where),
        login_server=tailscale.get("login_server"),
        tailscale_enabled="tailscale" in d,
        raw=d,
    )
    _validate(cfg)
    # Normalize the canonical `v` prefix on both component versions: an
    # unprefixed value passes the version regex but would otherwise be compared
    # verbatim against talosctl/kubeconfig output and rendered into image tags
    # and factory URLs, judging every node outdated forever (the server's
    # `v1.31.0` never equals a `1.31.0` pin) and producing an untagged
    # kube-proxy image reference. This runs after _validate so a non-string
    # version still raises ConfigError.
    if not cfg.talos_version.startswith("v"):
        cfg.talos_version = f"v{cfg.talos_version}"
    if not cfg.kubernetes_version.startswith("v"):
        cfg.kubernetes_version = f"v{cfg.kubernetes_version}"
    return cfg


def load_secrets(root: Path) -> Secrets:
    d = read_yaml(root / SECRETS_FILE)
    where = SECRETS_FILE
    _reject_unknown_keys(d, where, _SECRETS_KEYS | _plugin_config_sections())
    cluster = read_yaml(root / CLUSTER_FILE)
    selected = [name for name in ("openstack", "proxmox") if name in cluster]
    if len(selected) != 1:
        raise ConfigError(
            f"{CLUSTER_FILE}: exactly one provider section is required: openstack or proxmox"
        )
    provider_name = selected[0]
    other = "proxmox" if provider_name == "openstack" else "openstack"
    if provider_name not in d or other in d:
        raise ConfigError(
            f"{where}: {provider_name} credentials must match the {CLUSTER_FILE} provider"
        )
    provider_data = _mapping(d[provider_name], f"{where}: {provider_name}")
    _reject_unknown_keys(
        provider_data, f"{where}: {provider_name}", _SECRETS_SECTION_KEYS[provider_name]
    )
    if provider_name == "openstack":
        provider: ProviderSecrets = OpenStackSecrets(
            credential_id=_secret(
                "openstack.credential_id",
                require(provider_data, "credential_id", where=f"{where}: openstack"),
                where=f"{where}: openstack",
            ),
            credential_secret=_secret(
                "openstack.credential_secret",
                require(provider_data, "credential_secret", where=f"{where}: openstack"),
                where=f"{where}: openstack",
            ),
        )
    else:
        provider = ProxmoxSecrets(
            token_id=_secret(
                "proxmox.token_id",
                require(provider_data, "token_id", where=f"{where}: proxmox"),
                where=f"{where}: proxmox",
            ),
            token_secret=_secret(
                "proxmox.token_secret",
                require(provider_data, "token_secret", where=f"{where}: proxmox"),
                where=f"{where}: proxmox",
            ),
        )
    ts = _mapping(d.get("tailscale"), f"{where}: tailscale")
    _reject_unknown_keys(ts, f"{where}: tailscale", _SECRETS_SECTION_KEYS["tailscale"])
    auth_key = ts.get("auth_key")
    return Secrets(
        provider=provider,
        tailscale_auth_key=(
            None if auth_key is None else _secret("tailscale.auth_key", auth_key, where)
        ),
    )


def _int(value: Any, field: str, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")


def _validate_proxmox_external(
    ext: dict[str, Any], cluster_net: ipaddress.IPv4Network
) -> None:
    """Validate proxmox.network.external for directly routed API and ingress."""
    bridge = ext.get("bridge")
    if not isinstance(bridge, str) or not bridge.strip():
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.bridge must be a non-empty string"
        )

    ext_cidr = ext.get("cidr")
    if not isinstance(ext_cidr, str) or not ext_cidr.strip():
        raise ConfigError("cluster.yaml: proxmox.network.external.cidr must be an IPv4 CIDR")
    try:
        ext_network = ipaddress.ip_network(ext_cidr, strict=False)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: proxmox.network.external.cidr is invalid: {ext_cidr!r}"
        ) from None
    if ext_network.version != 4:
        raise ConfigError("cluster.yaml: proxmox.network.external.cidr must be IPv4")
    if ext_network.overlaps(cluster_net):
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.cidr must not overlap network.cidr"
        )

    gateway = ext.get("gateway")
    if not isinstance(gateway, str) or not gateway.strip():
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.gateway must be an IPv4 address"
        )
    try:
        gw = ipaddress.ip_address(gateway)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: proxmox.network.external.gateway is invalid: {gateway!r}"
        ) from None
    if gw.version != 4 or gw not in ext_network:
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.gateway must be inside external.cidr"
        )

    anchor_cidr = ext.get("anchor_cidr")
    if not isinstance(anchor_cidr, str) or not anchor_cidr.strip():
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.anchor_cidr must be an IPv4 CIDR"
        )
    try:
        anchor_net = ipaddress.ip_network(anchor_cidr, strict=False)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: proxmox.network.external.anchor_cidr is invalid: {anchor_cidr!r}"
        ) from None
    link_local = ipaddress.IPv4Network("169.254.0.0/16")
    if not isinstance(anchor_net, ipaddress.IPv4Network):
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.anchor_cidr must be IPv4"
        )
    if not anchor_net.subnet_of(link_local):
        raise ConfigError(
            "cluster.yaml: proxmox.network.external.anchor_cidr must be inside 169.254.0.0/16"
        )

    kubeapi_vip = ext.get("kubeapi_vip")
    if kubeapi_vip is not None:
        if not isinstance(kubeapi_vip, str) or not kubeapi_vip.strip():
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.kubeapi_vip must be an IPv4 address"
            )
        try:
            vip = ipaddress.ip_address(kubeapi_vip)
        except (TypeError, ValueError):
            raise ConfigError(
                f"cluster.yaml: proxmox.network.external.kubeapi_vip is invalid: {kubeapi_vip!r}"
            ) from None
        if vip.version != 4 or vip not in ext_network:
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.kubeapi_vip must be inside external.cidr"
            )

    vlan = ext.get("vlan")
    if vlan is not None and not 1 <= _int(vlan, "vlan", "proxmox.network.external") <= 4094:
        raise ConfigError("cluster.yaml: proxmox.network.external.vlan must be 1-4094")

    ingress_pool = ext.get("ingress_pool")
    if ingress_pool is not None:
        if not isinstance(ingress_pool, str) or not ingress_pool.strip():
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.ingress_pool must be a string"
            )
        parts = ingress_pool.split("-")
        if len(parts) != 2:
            raise ConfigError(
                f"proxmox.network.external.ingress_pool must be 'start-end', got {ingress_pool!r}"
            )
        try:
            pool_start = ipaddress.ip_address(parts[0].strip())
            pool_end = ipaddress.ip_address(parts[1].strip())
        except ValueError:
            raise ConfigError(
                f"proxmox.network.external.ingress_pool has invalid addresses: {ingress_pool!r}"
            ) from None
        if pool_start.version != 4 or pool_end.version != 4:
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.ingress_pool must be IPv4"
            )
        if int(pool_start) > int(pool_end):
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.ingress_pool start must be <= end"
            )
        if pool_start not in ext_network or pool_end not in ext_network:
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.ingress_pool must be inside external.cidr"
            )
        if kubeapi_vip is not None and int(pool_start) <= int(vip) <= int(pool_end):
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.kubeapi_vip must not be inside "
                "ingress_pool (MetalLB could hand the API address to a service)"
            )


def _validate_proxmox_sdn(raw: Any, cfg: Config, cluster_vip: Any) -> None:
    """Validate proxmox.network.cluster.sdn (managed EVPN) and its derived layout."""
    where = "cluster.yaml: proxmox.network.cluster.sdn"
    sdn_map = _mapping(raw, where)
    provider = cfg.provider
    assert isinstance(provider, ProxmoxConfig)

    sdn_name = sdn_map.get("name")
    if sdn_name is not None and (not isinstance(sdn_name, str) or not sdn_name.strip()):
        raise ConfigError(f"{where}.name must be a non-empty string")
    effective_name = str(sdn_name or cfg.name)
    if not naming.SDN_ID_RE.fullmatch(effective_name):
        raise ConfigError(
            f"cluster.yaml: {effective_name!r} cannot be used as the SDN zone/VNet "
            "id: it must be 2-8 characters, start with a letter, and contain no "
            "hyphens (set proxmox.network.cluster.sdn.name to override the "
            "cluster-name default)"
        )
    zone = sdn_map.get("zone")
    if zone is not None and zone != "evpn":
        raise ConfigError(
            f"{where}.zone only supports 'evpn' (a plain VXLAN zone has no gateway, "
            "SNAT, or DHCP; VLAN and simple zones are deferred)"
        )
    controller = sdn_map.get("controller")
    if controller is not None and (not isinstance(controller, str) or not controller.strip()):
        raise ConfigError(f"{where}.controller must be a non-empty string")
    asn = sdn_map.get("asn")
    if asn is not None and not 0 <= _int(asn, "asn", where) < 2**32:
        raise ConfigError(f"{where}.asn must be 0-4294967295")
    for field_name in ("vrf_tag", "tag"):
        value = sdn_map.get(field_name)
        if value is not None and not (
            naming.SDN_VNI_MIN <= _int(value, field_name, where) <= naming.SDN_VNI_MAX
        ):
            raise ConfigError(f"{where}.{field_name} must be 1-16777215")
    mtu = sdn_map.get("mtu")
    if mtu is not None and _int(mtu, "mtu", where) <= 0:
        raise ConfigError(f"{where}.mtu must be greater than zero")
    nodes = _string_list(sdn_map.get("nodes"), f"{where}.nodes")
    exit_nodes = _string_list(sdn_map.get("exit_nodes"), f"{where}.exit_nodes")
    primary = sdn_map.get("primary_exit_node")
    if primary is not None and (not isinstance(primary, str) or not primary.strip()):
        raise ConfigError(f"{where}.primary_exit_node must be a non-empty string")

    resolved = proxmox_sdn(cfg.name, provider)
    assert resolved is not None
    if not naming.SDN_VNI_MIN <= resolved.tag <= naming.SDN_VNI_MAX:
        raise ConfigError(
            f"{where}: the default tag (vrf_tag + 1 = {resolved.tag}) is outside "
            "1-16777215; set an explicit tag"
        )
    if resolved.tag == resolved.vrf_tag:
        raise ConfigError(f"{where}.tag must differ from vrf_tag")
    if nodes:
        outside = sorted(set(exit_nodes) - set(nodes))
        if outside:
            raise ConfigError(
                f"{where}.exit_nodes must be members of sdn.nodes: " + ", ".join(outside)
            )
        # every compute node needs the VNet bridge, so placement must stay
        # inside the zone -- not the other way around
        unplaceable = sorted(set(provider.nodes) - set(nodes)) if provider.nodes else []
        if unplaceable:
            raise ConfigError(
                f"{where}.nodes must include every proxmox.nodes entry "
                "(VMs placed outside the zone have no bridge): " + ", ".join(unplaceable)
            )
    if resolved.exit_nodes and resolved.primary_exit_node not in resolved.exit_nodes:
        raise ConfigError(f"{where}.primary_exit_node must be one of the exit nodes")
    if not cfg.network.dns:
        raise ConfigError(
            "cluster.yaml: network.dns is required with proxmox.network.cluster.sdn "
            "(static addressing has no DHCP-provided DNS)"
        )

    # static address layout: node_address raises on overflow; check VIP collisions
    cidr = cfg.network.cluster.cidr
    gateway = naming.sdn_gateway(cidr)
    worker_pools = tuple(cfg.workers)
    addresses = {
        m.name: naming.node_address(cidr, m.name, m.role, m.pool, worker_pools).ip
        for m in cfg.machines.values()
    }
    if isinstance(cluster_vip, str):
        try:
            vip = ipaddress.ip_address(cluster_vip)
        except ValueError:
            return  # the main provider block reports invalid VIPs
        if vip == gateway:
            raise ConfigError(
                "cluster.yaml: proxmox.network.cluster.kubeapi_vip collides with the "
                f"SDN anycast gateway {gateway}"
            )
        collision = next((name for name, addr in addresses.items() if addr == vip), None)
        if collision:
            raise ConfigError(
                "cluster.yaml: proxmox.network.cluster.kubeapi_vip collides with the "
                f"static address of {collision}"
            )
        # the layout reserves slots for nodes a pool has not grown to yet;
        # a VIP parked there collides the moment that node is added
        if vip in naming.sdn_reserved(cidr, worker_pools):
            raise ConfigError(
                "cluster.yaml: proxmox.network.cluster.kubeapi_vip sits inside the "
                "SDN static address layout (controlplane range or a worker pool "
                "block); scaling a pool would assign a node the VIP's address"
            )


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
    if versions.is_older(cfg.talos_version, MIN_TALOS_VERSION):
        raise ConfigError(
            f"cluster.yaml: talos.version must be {MIN_TALOS_VERSION} or newer, "
            f"got {cfg.talos_version}"
        )

    if "controlplane" in cfg.workers:
        raise ConfigError("worker pool name 'controlplane' is reserved")

    pools = {"controlplane": cfg.controlplane, **cfg.workers}
    for pool_name, p in pools.items():
        if not isinstance(pool_name, str) or not _NAME_RE.fullmatch(pool_name):
            raise ConfigError(f"worker pool name {pool_name!r} is not a valid hostname component")
        if not isinstance(p, dict):
            raise ConfigError(f"pool '{pool_name}' must be a YAML mapping")
        required = (
            ("count", "flavor", "disk")
            if isinstance(cfg.provider, OpenStackConfig)
            else ("count", "cores", "memory", "disk")
        )
        for key in required:
            if key not in p:
                raise ConfigError(f"pool '{pool_name}' missing '{key}'")
        count = _int(p["count"], "count", f"pool '{pool_name}'")
        if pool_name == "controlplane" and count < 1:
            raise ConfigError("pool 'controlplane': 'count' must be at least 1")
        if pool_name != "controlplane" and count < 0:
            raise ConfigError(f"pool '{pool_name}': 'count' must be zero or greater")
        if isinstance(cfg.provider, OpenStackConfig):
            if not isinstance(p["flavor"], str) or not p["flavor"].strip():
                raise ConfigError(f"pool '{pool_name}': 'flavor' must be a non-empty string")
        else:
            if _int(p["cores"], "cores", f"pool '{pool_name}'") <= 0:
                raise ConfigError(f"pool '{pool_name}': 'cores' must be greater than zero")
            if _int(p["memory"], "memory", f"pool '{pool_name}'") <= 0:
                raise ConfigError(f"pool '{pool_name}': 'memory' must be greater than zero")
            node = p.get("node")
            if node is not None and (not isinstance(node, str) or not node.strip()):
                raise ConfigError(f"pool '{pool_name}': 'node' must be a non-empty string")
            if node and cfg.provider.nodes and node not in cfg.provider.nodes:
                raise ConfigError(
                    f"pool '{pool_name}': node {node!r} is not in proxmox.nodes"
                )
        if _int(p["disk"], "disk", f"pool '{pool_name}'") <= 0:
            raise ConfigError(f"pool '{pool_name}': 'disk' must be greater than zero")
        _string_list(p.get("extensions"), f"pool '{pool_name}': extensions")
        _string_list(p.get("config_patches"), f"pool '{pool_name}': config_patches")
        _mapping(p.get("tags"), f"pool '{pool_name}': tags")
        # `count` gives the widest ordinal a pool can reach: 3 digits at 100+, so
        # the -01 sentinel of a small pool would under-measure pools of 100+.
        if len(f"{cfg.name}-{pool_name}-{count:02d}") > 63:
            raise ConfigError("cluster and pool names make a hostname longer than 63 characters")

    try:
        network = ipaddress.ip_network(cfg.network.cluster.cidr, strict=True)
    except (TypeError, ValueError):
        raise ConfigError(
            f"cluster.yaml: network.cidr is not a valid network: {cfg.network.cluster.cidr!r}"
        ) from None
    if network.version != 4:
        raise ConfigError("cluster.yaml: network.cidr must be IPv4")

    provider_fields = (
        (("openstack.url", cfg.provider.url),
         ("openstack.availability_zone", cfg.provider.availability_zone),
         ("openstack.external_net", cfg.provider.external_net))
        if isinstance(cfg.provider, OpenStackConfig)
        else (("proxmox.url", cfg.provider.url),)
    )
    for field_name, value in provider_fields:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"cluster.yaml: {field_name} must be a non-empty string")
    if isinstance(cfg.provider, ProxmoxConfig):
        provider = cfg.provider
        for field_name, value in (
            ("proxmox.storage", provider.storage),
            ("proxmox.iso_storage", provider.iso_storage),
            ("proxmox.cidata_storage", provider.cidata_storage),
        ):
            if not value.strip():
                raise ConfigError(f"cluster.yaml: {field_name} must be a non-empty string")
        if provider.placement_strategy != "spread":
            raise ConfigError("cluster.yaml: proxmox.placement_strategy must be 'spread'")
        if not isinstance(provider.tls_verify, (bool, str)) or provider.tls_verify == "":
            raise ConfigError(
                "cluster.yaml: proxmox.tls_verify must be true, false, or a CA bundle path"
            )
        _reject_unknown_keys(
            provider.network, "cluster.yaml: proxmox.network", _PROXMOX_NETWORK_KEYS
        )
        cluster_network = _mapping(
            provider.network.get("cluster"), "cluster.yaml: proxmox.network.cluster"
        )
        links = [name for name in ("bridge", "vnet") if cluster_network.get(name)]
        if "sdn" in cluster_network:
            if links or cfg.network.cluster.vlan is not None:
                raise ConfigError(
                    "cluster.yaml: proxmox.network.cluster.sdn is mutually exclusive "
                    "with bridge, vnet, and vlan"
                )
            sdn_map = _mapping(
                cluster_network.get("sdn"), "cluster.yaml: proxmox.network.cluster.sdn"
            )
            _reject_unknown_keys(
                sdn_map, "cluster.yaml: proxmox.network.cluster.sdn", _PROXMOX_SDN_KEYS
            )
            _validate_proxmox_sdn(
                sdn_map, cfg, cfg.network.cluster.kubeapi_vip or None
            )
        elif len(links) != 1:
            raise ConfigError(
                "cluster.yaml: proxmox.network.cluster requires exactly one of "
                "bridge, vnet, or sdn"
            )
        vlan = cluster_network.get("vlan")
        if vlan is not None and not 1 <= _int(vlan, "vlan", "proxmox.network.cluster") <= 4094:
            raise ConfigError("cluster.yaml: proxmox.network.cluster.vlan must be 1-4094")

        external_network = _mapping(
            provider.network.get("external"), "cluster.yaml: proxmox.network.external"
        )
        # the VIPs are taken from the resolved blocks, so either location -- the
        # `network.*` blocks or the old `proxmox.network.*` keys -- satisfies the
        # rule that exactly one of them carries the API address
        cluster_vip = cfg.network.cluster.kubeapi_vip
        external_vip = cfg.network.external.kubeapi_vip if cfg.network.external else ""
        if cfg.network.external is not None:
            # validate the resolved facts, so a block split between the new
            # `network.external` keys and the Proxmox section is checked whole
            _validate_proxmox_external(
                {**external_network, **l2_facts(cfg.network.external)}, network
            )
            if not cluster_vip and not external_vip:
                raise ConfigError(
                    "cluster.yaml: kubeapi_vip must be set in network.cluster or network.external"
                )
        elif not cluster_vip:
            raise ConfigError(
                "cluster.yaml: network.cluster.kubeapi_vip must be an IPv4 address"
            )
        if cluster_vip:
            vip = _ipv4_address(cluster_vip, "network.cluster.kubeapi_vip")
            if vip not in network:
                raise ConfigError(
                    "cluster.yaml: network.cluster.kubeapi_vip must be inside network.cidr"
                )
    if cfg.login_server is not None and not isinstance(cfg.login_server, str):
        raise ConfigError("cluster.yaml: tailscale.login_server must be a string")

    for dns in cfg.network.dns:
        try:
            ipaddress.ip_address(dns)
        except ValueError:
            raise ConfigError(
                f"cluster.yaml: network.dns contains an invalid address: {dns!r}"
            ) from None
    for rule in cfg.security.values():
        field_name = f"security.{rule.name}"
        for label, cidr in rule.hosts.items():
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
    if (
        isinstance(cfg.provider, ProxmoxConfig)
        and cfg.network.dns
        and not proxmox_sdn(cfg.name, cfg.provider)
    ):
        warnings.append(
            "network.dns is ignored on a Proxmox bridge/vnet (DHCP-backed) network; "
            "nodes get their DNS from the DHCP server, so the configured resolvers "
            "are not applied"
        )
    return warnings
