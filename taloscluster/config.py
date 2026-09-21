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
# CONFIG_SECTIONS; `openstack`/`proxmox` is the optional VM provider and
# `metal` the optional bare-metal section beside (or instead of) it.
_CLUSTER_KEYS = {
    "name", "tags", "talos", "kubernetes", "controlplane", "workers",
    "network", "security", "tailscale", "openstack", "proxmox", "metal",
    "include",
}
# Direct keys each fixed-schema section of `cluster.yaml` accepts. These catch
# a miscapped or unsupported key inside a section -- `talos.extensons`,
# `network.dnss`, `openstack.regoin` -- that the top-level allowlist alone would
# let load and be silently ignored. Freeform maps are deliberately not
# enumerated here: `tags`/pool `tags` and security host labels are label maps,
# and `config_patches` hold freeform YAML documents.
_TALOS_KEYS = {"version", "extensions", "config_patches", "kubespan"}
_NETWORK_KEYS = {"dns", "ntp", "cluster", "external"}
#: Direct keys an L2 block (`network.cluster`, `network.external`) accepts.
_L2_KEYS = {"cidr", "gateway", "vlan", "mtu", "kubeapi_vip"}
#: L2 keys that only describe the externally routed network, never the node L2.
_L2_EXTERNAL_ONLY_KEYS = {"anchor_cidr", "ingress_pool"}
_KUBERNETES_KEYS = {"version"}
_TAILSCALE_KEYS = {"login_server", "auth_key"}
_PROVIDER_KEYS = {
    "openstack": {"url", "availability_zone", "external_net", "region",
                  "credential_id", "credential_secret"},
    "proxmox": {"url", "storage", "iso_storage", "cidata_storage",
                "placement_strategy", "nodes", "tls_verify", "network",
                "token_id", "token_secret"},
}
#: Direct keys `proxmox.network` accepts. Both subsections are fixed-schema, so
#: a miscapped `clustr`/`extrnl` section is refused instead of ignored.
_PROXMOX_NETWORK_KEYS = {"cluster", "external"}
#: Direct keys `proxmox.network.cluster` accepts; exactly one of `bridge`,
#: `vnet` or `sdn` is required (see :func:`_validate`). The addresses on that
#: network live in `network.cluster`.
_PROXMOX_CLUSTER_KEYS = {"bridge", "vnet", "sdn"}
#: Direct keys `proxmox.network.cluster.sdn` accepts.
_PROXMOX_SDN_KEYS = {"name", "zone", "controller", "asn", "vrf_tag", "tag",
                     "mtu", "nodes", "exit_nodes", "primary_exit_node"}
#: Direct keys `proxmox.network.external` accepts; the addresses on that
#: network live in `network.external`.
_PROXMOX_EXTERNAL_KEYS = {"bridge"}
#: Direct keys a `metal` group accepts. A group is the defaults its `servers`
#: start from: each server carries the same keys and overrides its own.
_METAL_GROUP_KEYS = {"role", "redfish", "disk", "network", "interfaces", "bmc",
                     "servers"}
#: Direct keys one `metal.<group>.servers` entry accepts: the group settings it
#: may override, minus the servers list itself.
_METAL_SERVER_KEYS = _METAL_GROUP_KEYS - {"servers"}
#: Direct keys one `metal.<group>.interfaces` entry accepts.
_METAL_INTERFACE_KEYS = {"role", "ip", "dns", "link_name", "vlan"}
#: Direct keys a `metal.<group>.bmc` block accepts.
_METAL_BMC_KEYS = {"ip", "username", "password"}
#: What an interface's `role` may say, as a bare value or a list of them.
_METAL_INTERFACE_ROLES = ("cluster", "external", "pxe")
#: Keys that moved into the `network` blocks, by the section they used to live
#: in, so an old cluster.yaml is refused with the new location rather than a
#: bare "unknown key".
_MOVED_KEYS: dict[tuple[str, ...], dict[str, str]] = {
    ("network",): {"cidr": "network.cluster.cidr"},
    ("proxmox", "network", "cluster"): {
        key: f"network.cluster.{key}" for key in ("vlan", "kubeapi_vip")
    },
    ("proxmox", "network", "external"): {
        key: f"network.external.{key}"
        for key in ("cidr", "gateway", "anchor_cidr", "kubeapi_vip", "vlan",
                    "ingress_pool")
    },
}
#: Keys a pool may carry; `tags` is a freeform label map and both `extensions` /
#: `config_patches` are freeform lists, so only the structural keys are fixed.
_POOL_KEYS = {"count", "flavor", "disk", "cores", "memory", "node",
              "extensions", "config_patches", "tags"}
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
# The WireGuard port Talos KubeSpan peers exchange handshakes on. The firewalls
# open it explicitly from the node L2s the all-port intra-cluster rules already
# admit, so the allowance survives those rules being narrowed.
KUBESPAN_PORT = 51820
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
    # application credential; scaffolded into secrets.yaml, which is merged in.
    # kept out of the repr so a traceback or a debug print cannot leak it
    credential_id: str = field(default="", repr=False)
    credential_secret: str = field(default="", repr=False)

    def credentials(self) -> tuple[str, str]:
        """The application credential, refusing a missing or placeholder value."""
        return (
            _require_secret("openstack.credential_id", self.credential_id),
            _require_secret("openstack.credential_secret", self.credential_secret),
        )


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
    # api token; scaffolded into secrets.yaml, which is merged in; kept out of
    # the repr so a traceback or a debug print cannot leak it
    token_id: str = field(default="", repr=False)
    token_secret: str = field(default="", repr=False)

    def credentials(self) -> tuple[str, str]:
        """The API token, refusing a missing or placeholder value."""
        return (
            _require_secret("proxmox.token_id", self.token_id),
            _require_secret("proxmox.token_secret", self.token_secret),
        )


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
class MetalConfig:
    """The `metal:` section: groups of bare-metal machines beside the cluster."""

    groups: dict[str, MetalGroup] = field(default_factory=dict)


@dataclass(frozen=True)
class MetalInterface:
    """One NIC of a bare-metal machine, keyed by its OS interface name.

    `pxe` marks the boot/maintenance link, `cluster` the node L2 and `external`
    the externally routed one; one interface may carry several roles. On an
    `external` link the generated configuration rides a VLAN child of the
    parent port, named `link_name` (default `<parent>.<vlan>`) and tagged
    `vlan` (default `network.external.vlan`).
    """

    role: tuple[str, ...] = ()
    ip: str = ""                       # static address, with its prefix length
    dns: tuple[str, ...] = ()
    link_name: str = ""                # external VLAN child link name override
    vlan: int | None = None            # external VLAN id override


@dataclass(frozen=True)
class MetalBmc:
    """The Redfish controller of one bare-metal machine.

    username/password are kept out of the repr like the provider credentials.
    """

    ip: str = ""
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class MetalGroup:
    """One `metal:` group: the defaults every server in `servers` starts from."""

    name: str
    role: str                          # controlplane | worker
    disk: str                          # install disk device
    network: L2Network                 # the group's node L2
    redfish: bool = False
    interfaces: dict[str, MetalInterface] = field(default_factory=dict)
    bmc: MetalBmc = field(default_factory=MetalBmc)
    servers: dict[str, MetalServer] = field(default_factory=dict)


@dataclass(frozen=True)
class MetalServer:
    """One bare-metal machine: its group's defaults with its overrides merged in."""

    name: str
    group: str
    role: str                          # controlplane | worker
    disk: str                          # install disk device
    network: L2Network                 # the machine's node L2
    redfish: bool = False
    interfaces: dict[str, MetalInterface] = field(default_factory=dict)
    bmc: MetalBmc = field(default_factory=MetalBmc)


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

    provider: ProviderConfig | None
    # bare-metal groups joined alongside (or instead of) the VM provider
    metal: MetalConfig | None

    network: NetworkConfig

    # named ingress allowlists, in cluster.yaml order
    security: dict[str, SecurityRule]

    login_server: str | None           # headscale/tailscale login server
    # pre-auth key nodes register with; None leaves the extension idle, and it
    # stays out of the repr like the provider credentials
    auth_key: str | None = field(default=None, repr=False)
    # a `tailscale:` section in cluster.yaml opts the installed system into the
    # tailscale extension; the shared boot ISO always carries it either way
    tailscale_enabled: bool = True
    # talos.kubespan: true emits machine.network.kubespan on every node; false
    # (the default) emits no kubespan settings at all, and _validate requires
    # true when a metal group sits on another L2
    kubespan: bool = False

    # the merged cluster.yaml + secrets.yaml + include tree: it carries the
    # credentials verbatim, so never print or serialize it
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived ------------------------------------------------------------

    @property
    def tailscale_auth_key(self) -> str | None:
        """The pre-auth key, or None when the extension is left idle.

        Refuses a scaffolded or empty key here rather than letting every node
        fail to register: a cluster that configures a key means to use it.
        """
        if self.auth_key is None:
            return None
        return _require_secret("tailscale.auth_key", self.auth_key)

    @property
    def provider_name(self) -> str:
        """The VM provider's name, or an empty string without one."""
        if isinstance(self.provider, OpenStackConfig):
            return "openstack"
        if isinstance(self.provider, ProxmoxConfig):
            return "proxmox"
        return ""

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
    def openstack_credentials(self) -> tuple[str, str]:
        return self._openstack.credentials()

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

    def intra_cluster_cidrs(self, node_cidr: str | None = None) -> list[str]:
        """Every L2 the cluster's nodes sit on, for the firewalls' intra-cluster
        rules: `network.cluster.cidr` plus each `metal` group's own. `node_cidr`
        adds the L2 of a node sitting off the cluster network (a metal server),
        so a stack keyed on it still admits the group alongside the rest.
        Deduplicated, the cluster L2 first.
        """
        subnets = [self.network.cluster.cidr]
        if self.metal is not None:
            subnets.extend(group.network.cidr for group in self.metal.groups.values())
        if node_cidr:
            subnets.append(node_cidr)
        return list(dict.fromkeys(subnets))

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

    @cached_property
    def metal_servers(self) -> dict[str, str]:
        """Flat hostname -> role map of every `metal.<group>.servers` entry.

        The metal counterpart of `machines`: nodes the cluster must expect to
        be running -- scale-down must never treat one as a removal and check
        must verify it -- although no VM provider manages them.
        """
        if self.metal is None:
            return {}
        return {
            server.name: server.role
            for group in self.metal.groups.values()
            for server in group.servers.values()
        }

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


def _record_origins(value: Any, path: str, origins: dict[str, str], name: str) -> None:
    """Remember `name` as the source of `path` and of every path inside it."""
    origins[path] = name
    if isinstance(value, dict):
        for key, child in value.items():
            _record_origins(child, f"{path}.{key}", origins, name)


def _merge_yaml(
    base: dict[str, Any],
    extra: dict[str, Any],
    extra_name: str,
    origins: dict[str, str],
    base_name: str,
    prefix: str = "",
) -> None:
    """Deep-merge `extra` into `base`; a value set in two files is an error.

    Mappings merge key by key. Anything else -- a scalar, a list, or a mapping
    meeting a scalar -- is a single value, so two files setting it disagree
    about the desired state with no way to tell which one wins. `origins`
    remembers which file set each path so the error can name both.
    """
    for key, value in extra.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_yaml(base[key], value, extra_name, origins, base_name, f"{path}.")
            continue
        # an explicit null is no value at all, as everywhere else in the loader:
        # a comment-only section (`rancher:` with only comments under it) adds
        # nothing and collides with nothing
        if value is None:
            continue
        if base.get(key) is not None:
            raise ConfigError(
                f"{path} is set in both {origins.get(path, base_name)} and "
                f"{extra_name}; set it in one file"
            )
        base[key] = value
        _record_origins(value, path, origins, extra_name)


def _include_paths(d: dict[str, Any], root: Path, where: str) -> list[Path]:
    """The files `include:` names, resolved inside the cluster directory."""
    raw = d.get("include")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: include must be a list of file names")
    paths: list[Path] = []
    seen: dict[Path, str] = {}
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"{where}: include entries must be non-empty file names")
        relative = Path(entry)
        path = root / relative
        # resolve before the check so a symlink cannot lead out of the directory
        if relative.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            raise ConfigError(
                f"{where}: include {entry!r} must be a path inside the cluster directory"
            )
        if path.resolve() == (root / SECRETS_FILE).resolve():
            raise ConfigError(
                f"{where}: {SECRETS_FILE} is always included; do not list it"
            )
        if path.resolve() in seen:
            raise ConfigError(
                f"{where}: include lists {entry} twice"
                if seen[path.resolve()] == entry
                else f"{where}: include lists {seen[path.resolve()]} and {entry}, "
                "which are the same file"
            )
        seen[path.resolve()] = entry
        paths.append(path)
    return paths


def _apply_includes(
    d: dict[str, Any], root: Path, known: set[str]
) -> tuple[dict[str, Any], set[str]]:
    """Merge every `include:` file into the `cluster.yaml` tree.

    Returns the merged tree and the top-level sections the cluster opted into
    by hand -- everything but the ones only `secrets.yaml` contributes, which
    holds credentials for features, not the choice to use them.

    Included files carry the same keys as `cluster.yaml` and are merged before
    validation, so where a value lives is the user's choice and the schema is
    the same wherever it is written. Only the top-level keys of an included
    file are attributed to it; once merged there is one tree and one schema, so
    an unknown or moved key deeper in a section is reported against
    `cluster.yaml` whichever file supplied it.
    """
    origins: dict[str, str] = {}
    opted_in = set(d)
    # secrets.yaml is always included first when it exists, so credentials are
    # ordinary cluster keys that happen to live in a gitignored file
    secrets = root / SECRETS_FILE
    sources = ([secrets] if secrets.is_file() else []) + _include_paths(
        d, root, CLUSTER_FILE
    )
    for path in sources:
        if path.exists() and not path.is_file():
            raise ConfigError(f"{CLUSTER_FILE}: include {path.name} is not a file")
        extra = read_yaml(path)
        if "include" in extra:
            raise ConfigError(
                f"{path.name}: include is only allowed in {CLUSTER_FILE}; "
                "included files cannot include further files"
            )
        _reject_unknown_keys(extra, path.name, known - {"include"})
        if path != secrets:
            opted_in.update(extra)
        _merge_yaml(d, extra, path.name, origins, CLUSTER_FILE)
    return d, opted_in


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


def _require_secret(name: str, value: Any) -> str:
    """A credential the command about to run needs, wherever it was written."""
    return _secret(name, value or None, f"{SECRETS_FILE} (or any included file)")


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


def _metal_config(
    d: dict[str, Any], where: str, cluster: L2Network
) -> MetalConfig:
    """Parse the `metal:` section into typed groups, server merges applied."""
    raw = _mapping(d.get("metal"), f"{where}: metal")
    groups: dict[str, MetalGroup] = {}
    names: set[str] = set()
    for name, group in raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: metal group names must be non-empty strings")
        gwhere = f"{where}: metal.{name}"
        group = {k: v for k, v in _mapping(group, gwhere).items() if v is not None}
        _reject_unknown_keys(group, gwhere, _METAL_GROUP_KEYS)
        groups[name] = _metal_group(name, group, gwhere, cluster)
        duplicate = names.intersection(groups[name].servers)
        if duplicate:
            raise ConfigError(
                f"{where}: metal server {sorted(duplicate)[0]!r} is defined in "
                "more than one group"
            )
        names.update(groups[name].servers)
    return MetalConfig(groups=groups)


def _metal_group(
    name: str, group: dict[str, Any], where: str, cluster: L2Network
) -> MetalGroup:
    """One group: its parsed defaults plus every server merged over them."""
    parsed = _metal_fields(group, where, cluster)
    servers: dict[str, MetalServer] = {}
    for server_name, server in _mapping(
        group.get("servers"), f"{where}.servers"
    ).items():
        swhere = f"{where}.servers.{server_name}"
        if not isinstance(server_name, str) or not server_name:
            raise ConfigError(f"{where}.servers: server names must be non-empty strings")
        if not _NAME_RE.fullmatch(server_name):
            raise ConfigError(
                f"{swhere}: server name {server_name!r} is not a valid hostname component"
            )
        server = {k: v for k, v in _mapping(server, swhere).items() if v is not None}
        _reject_unknown_keys(server, swhere, _METAL_SERVER_KEYS)
        servers[server_name] = _metal_server(
            server_name, name, server, group, swhere, cluster
        )
    return MetalGroup(name=name, servers=servers, **parsed)


def _metal_server(
    name: str,
    group_name: str,
    server: dict[str, Any],
    group: dict[str, Any],
    where: str,
    cluster: L2Network,
) -> MetalServer:
    """One machine: the group defaults with the server's overrides merged in.

    Plain settings are replaced outright, while `bmc` merges key by key and
    `interfaces` merge per interface name -- the prototype's `load_machines`
    merge, so the group carries the credentials and the cabling plan and each
    server adds only its own addresses. The merged `bmc` is where a machine's
    credentials have settled, whichever file wrote each key, so a machine on a
    `redfish: true` group must end up with real ones here.
    """
    interfaces = {
        ifname: dict(iface)
        for ifname, iface in _mapping(
            group.get("interfaces"), f"{where}.interfaces"
        ).items()
    }
    for ifname, iface in _mapping(server.get("interfaces"), f"{where}.interfaces").items():
        interfaces.setdefault(ifname, {}).update(
            _mapping(iface, f"{where}.interfaces.{ifname}")
        )
    merged = {
        **{k: v for k, v in group.items() if k not in ("bmc", "interfaces", "servers")},
        **{k: v for k, v in server.items() if k not in ("bmc", "interfaces")},
        "bmc": {
            **_mapping(group.get("bmc"), f"{where}.bmc"),
            **_mapping(server.get("bmc"), f"{where}.bmc"),
        },
        "interfaces": interfaces,
    }
    if merged.get("redfish") is True:
        for key in ("username", "password"):
            _secret(f"bmc.{key}", merged["bmc"].get(key), where)
    return MetalServer(name=name, group=group_name, **_metal_fields(merged, where, cluster))


def _metal_fields(
    raw: dict[str, Any], where: str, cluster: L2Network
) -> dict[str, Any]:
    """The settings a group -- or a server merged over one -- carries, parsed."""
    return {
        "role": _metal_role(require(raw, "role", where=where), f"{where}.role"),
        "disk": _metal_disk(require(raw, "disk", where=where), f"{where}.disk"),
        "redfish": _metal_flag(raw.get("redfish", False), f"{where}.redfish"),
        "network": _metal_l2(
            _mapping(raw.get("network"), f"{where}.network"), f"{where}.network", cluster
        ),
        "interfaces": _metal_interfaces(raw.get("interfaces"), f"{where}.interfaces"),
        "bmc": _metal_bmc(_mapping(raw.get("bmc"), f"{where}.bmc"), f"{where}.bmc"),
    }


def _metal_role(value: Any, where: str) -> str:
    if value not in ("controlplane", "worker"):
        raise ConfigError(f"{where} must be 'controlplane' or 'worker'")
    return value


def _metal_flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def _metal_disk(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _metal_l2(raw: dict[str, Any], where: str, cluster: L2Network) -> L2Network:
    """A metal group's node L2: the explicit block, or `network.cluster` itself."""
    if not raw:
        return cluster
    _reject_unknown_keys(raw, where, _L2_KEYS)
    # _validate_l2 names cluster.yaml itself, so it takes the path without it
    return _l2_network(
        {k: v for k, v in raw.items() if v is not None},
        where.removeprefix(f"{CLUSTER_FILE}: "),
        external=False,
    )


def _metal_interfaces(raw: Any, where: str) -> dict[str, MetalInterface]:
    interfaces: dict[str, MetalInterface] = {}
    for ifname, iface in _mapping(raw, where).items():
        iwhere = f"{where}.{ifname}"
        if not isinstance(ifname, str) or not ifname:
            raise ConfigError(f"{where}: interface names must be non-empty strings")
        iface = {k: v for k, v in _mapping(iface, iwhere).items() if v is not None}
        _reject_unknown_keys(iface, iwhere, _METAL_INTERFACE_KEYS)
        interfaces[ifname] = MetalInterface(
            role=_metal_interface_role(
                require(iface, "role", where=iwhere), f"{iwhere}.role"
            ),
            ip=_metal_address(iface.get("ip"), f"{iwhere}.ip"),
            dns=_metal_resolvers(iface.get("dns"), f"{iwhere}.dns"),
            link_name=_metal_link_name(iface.get("link_name"), f"{iwhere}.link_name"),
            vlan=_metal_vlan(iface.get("vlan"), f"{iwhere}.vlan"),
        )
    return interfaces


def _metal_link_name(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _metal_vlan(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4094:
        raise ConfigError(f"{where} must be 1-4094")
    return value


def _metal_interface_role(value: Any, where: str) -> tuple[str, ...]:
    roles = (
        (value,)
        if isinstance(value, str)
        else tuple(value)
        if isinstance(value, list)
        else ()
    )
    if not roles or any(role not in _METAL_INTERFACE_ROLES for role in roles):
        raise ConfigError(
            f"{where} must be 'cluster', 'external', 'pxe', or a list of those"
        )
    return roles


def _metal_address(value: Any, where: str) -> str:
    """A static address, with or without its prefix length (`203.0.113.5/24`)."""
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be an IPv4 address with an optional /prefix")
    try:
        addr = ipaddress.ip_interface(value.strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{where} is invalid: {value!r}") from None
    if not isinstance(addr, ipaddress.IPv4Interface):
        raise ConfigError(f"{where} must be IPv4")
    return value


def _metal_resolvers(value: Any, where: str) -> tuple[str, ...]:
    resolvers = _string_list(value, where)
    for resolver in resolvers:
        try:
            ipaddress.ip_address(resolver)
        except ValueError:
            raise ConfigError(
                f"{where} contains an invalid address: {resolver!r}"
            ) from None
    return tuple(resolvers)


def _metal_bmc(raw: dict[str, Any], where: str) -> MetalBmc:
    _reject_unknown_keys(raw, where, _METAL_BMC_KEYS)
    ip = raw.get("ip")
    if ip is not None:
        _metal_address(ip, f"{where}.ip")
    fields: dict[str, str] = {}
    for key in ("username", "password"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError(f"{where}.{key} must be a non-empty string")
        fields[key] = value or ""
    return MetalBmc(ip=ip or "", username=fields["username"], password=fields["password"])


def _provider_config(
    d: dict[str, Any], where: str, cluster: L2Network
) -> tuple[ProviderConfig | None, MetalConfig | None]:
    """The selected VM provider plus the optional `metal` section.

    One VM provider (openstack or proxmox) is required; a `metal` section may
    sit beside it. Which machines land on which side is a per-pool decision the
    rest of the config is not asked to make yet.
    A metal group without its own `network` sits on the cluster L2, so the
    parsed blocks arrive here.
    """
    vm = [name for name in ("openstack", "proxmox") if name in d]
    if len(vm) > 1:
        raise ConfigError(
            f"{where}: at most one VM provider section is allowed: openstack or proxmox"
        )
    if not vm and "metal" not in d:
        raise ConfigError(
            f"{where}: one provider section is required: openstack, proxmox or metal"
        )

    provider: ProviderConfig | None = None
    if vm:
        name = vm[0]
        provider_map = _mapping(d[name], f"{where}: {name}")
        _reject_unknown_keys(provider_map, f"{where}: {name}", _PROVIDER_KEYS[name])
        if name == "openstack":
            provider = OpenStackConfig(
                url=require(provider_map, "url", where=f"{where}: openstack"),
                availability_zone=require(
                    provider_map, "availability_zone", where=f"{where}: openstack"
                ),
                external_net=require(
                    provider_map, "external_net", where=f"{where}: openstack"
                ),
                region=str(provider_map.get("region") or "RegionOne"),
                credential_id=provider_map.get("credential_id") or "",
                credential_secret=provider_map.get("credential_secret") or "",
            )
        else:
            provider = ProxmoxConfig(
                url=require(provider_map, "url", where=f"{where}: proxmox"),
                storage=str(provider_map.get("storage") or ""),
                iso_storage=str(provider_map.get("iso_storage") or ""),
                cidata_storage=str(provider_map.get("cidata_storage") or "local"),
                placement_strategy=str(
                    provider_map.get("placement_strategy") or "spread"
                ),
                nodes=tuple(
                    _string_list(provider_map.get("nodes"), f"{where}: proxmox.nodes")
                ),
                tls_verify=provider_map.get("tls_verify", True),
                network=_mapping(provider_map.get("network"), f"{where}: proxmox.network"),
                token_id=provider_map.get("token_id") or "",
                token_secret=provider_map.get("token_secret") or "",
            )
    metal = _metal_config(d, where, cluster) if "metal" in d else None
    return provider, metal


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


def _validate_l2(block: dict[str, Any], where: str, *, external: bool) -> None:
    """Validate one `network.cluster` / `network.external` block.

    An external block is only usable when it is fully described, so `cidr`,
    `gateway` and `anchor_cidr` are required there; a cluster block needs only
    its `cidr`.
    """
    cidr = _ipv4_network(block.get("cidr"), f"{where}.cidr", strict=not external)
    if external or "gateway" in block:
        gateway = _ipv4_address(block.get("gateway"), f"{where}.gateway")
        if gateway not in cidr:
            raise ConfigError(f"cluster.yaml: {where}.gateway must be inside {where}.cidr")
    if "vlan" in block and not 1 <= _int(block["vlan"], "vlan", f"cluster.yaml: {where}") <= 4094:
        raise ConfigError(f"cluster.yaml: {where}.vlan must be 1-4094")
    if "mtu" in block and _int(block["mtu"], "mtu", f"cluster.yaml: {where}") < MIN_MTU:
        raise ConfigError(f"cluster.yaml: {where}.mtu must be {MIN_MTU} or greater")
    vip: ipaddress.IPv4Address | None = None
    if "kubeapi_vip" in block:
        vip = _ipv4_address(block["kubeapi_vip"], f"{where}.kubeapi_vip")
        if vip not in cidr:
            raise ConfigError(
                f"cluster.yaml: {where}.kubeapi_vip must be inside {where}.cidr"
            )
    if external:
        anchor = _ipv4_network(
            block.get("anchor_cidr"), f"{where}.anchor_cidr", strict=False
        )
        if not anchor.subnet_of(ipaddress.IPv4Network("169.254.0.0/16")):
            raise ConfigError(
                f"cluster.yaml: {where}.anchor_cidr must be inside 169.254.0.0/16"
            )
    if "ingress_pool" in block:
        start, end = _ip_range(block["ingress_pool"], f"{where}.ingress_pool")
        if start not in cidr or end not in cidr:
            raise ConfigError(
                f"cluster.yaml: {where}.ingress_pool must be inside {where}.cidr"
            )
        if vip is not None and start <= vip <= end:
            raise ConfigError(
                f"cluster.yaml: {where}.kubeapi_vip must not be inside ingress_pool "
                "(MetalLB could hand the API address to a service)"
            )


def _l2_network(block: dict[str, Any], where: str, *, external: bool) -> L2Network:
    """One validated L2 block."""
    _validate_l2(block, where, external=external)
    return L2Network(
        cidr=str(block["cidr"]),
        gateway=str(block.get("gateway", "")),
        vlan=_int(block["vlan"], "vlan", f"cluster.yaml: {where}")
        if "vlan" in block
        else None,
        mtu=_int(block["mtu"], "mtu", f"cluster.yaml: {where}")
        if "mtu" in block
        else DEFAULT_MTU,
        kubeapi_vip=str(block.get("kubeapi_vip", "")),
        anchor_cidr=str(block.get("anchor_cidr", "")),
        ingress_pool=str(block.get("ingress_pool", "")),
    )


def _reject_moved_keys(d: dict[str, Any], where: str) -> None:
    """Refuse a key that moved into the `network` blocks, naming its new home."""
    for path, moved in _MOVED_KEYS.items():
        section: Any = d
        for part in path:
            section = section.get(part) if isinstance(section, dict) else None
        if not isinstance(section, dict):
            continue
        for key in sorted(section):
            if key in moved:
                raise ConfigError(
                    f"{where}: {'.'.join(path)}.{key} has moved to {moved[key]}"
                )


def _network_config(d: dict[str, Any], where: str) -> NetworkConfig:
    """Parse `network:` into the cluster-wide settings and the two L2 blocks."""
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

    cluster = _l2_network(cluster_raw, "network.cluster", external=False)
    external = (
        _l2_network(external_raw, "network.external", external=True)
        if external_raw
        else None
    )
    if external is not None:
        if ipaddress.ip_network(external.cidr, strict=False).overlaps(
            ipaddress.ip_network(cluster.cidr, strict=True)
        ):
            raise ConfigError(
                f"{where}: network.external.cidr must not overlap network.cluster.cidr"
            )
        if cluster.kubeapi_vip and external.kubeapi_vip:
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
    known = _CLUSTER_KEYS | _plugin_config_sections()
    _reject_unknown_keys(d, where, known)
    d, opted_in = _apply_includes(d, root, known)
    _reject_moved_keys(d, where)

    talos = _mapping(d.get("talos"), f"{where}: talos")
    _reject_unknown_keys(talos, f"{where}: talos", _TALOS_KEYS)
    kubespan = talos.get("kubespan")
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
    provider, metal = _provider_config(d, where, network.cluster)
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
        provider=provider,
        metal=metal,
        network=network,
        security=_security_rules(security, where),
        login_server=tailscale.get("login_server"),
        auth_key=tailscale.get("auth_key"),
        # a `tailscale:` section that only secrets.yaml carries is a leftover
        # credential, not a decision to run tailscale on the nodes
        tailscale_enabled="tailscale" in opted_in,
        # an explicit null is the same as an absent key, as everywhere else
        kubespan=False if kubespan is None else kubespan,
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


def _int(value: Any, field: str, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise ConfigError(f"{where}: '{field}' must be an integer, got {value!r}")


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
                "cluster.yaml: network.cluster.kubeapi_vip collides with the "
                f"SDN anycast gateway {gateway}"
            )
        collision = next((name for name, addr in addresses.items() if addr == vip), None)
        if collision:
            raise ConfigError(
                "cluster.yaml: network.cluster.kubeapi_vip collides with the "
                f"static address of {collision}"
            )
        # the layout reserves slots for nodes a pool has not grown to yet;
        # a VIP parked there collides the moment that node is added
        if vip in naming.sdn_reserved(cidr, worker_pools):
            raise ConfigError(
                "cluster.yaml: network.cluster.kubeapi_vip sits inside the "
                "SDN static address layout (controlplane range or a worker pool "
                "block); scaling a pool would assign a node the VIP's address"
            )


def _validate(cfg: Config) -> None:
    """Reject invalid or ambiguous desired state before touching the cluster."""
    if cfg.provider is None:
        # bare metal joins machines to a cluster a VM provider manages; with no
        # provider there is no backend, no bootstrap and no kubeconfig writer,
        # so a metal-only config is refused instead of failing mid-command
        raise ConfigError(
            "cluster.yaml: a metal section requires a VM provider section "
            "(openstack or proxmox); an all-bare-metal cluster is not supported"
        )
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
    if not isinstance(cfg.kubespan, bool):
        raise ConfigError("cluster.yaml: talos.kubespan must be true or false")
    if not cfg.kubespan and cfg.metal is not None:
        # a group on another L2 has no path to the cluster network without the
        # KubeSpan overlay, so turning it off there cannot converge; the L2 is
        # the subnet, so the VIP and MTU fields do not make a group remote. A
        # server may replace the group's network wholesale, so the merged
        # servers get the same check.
        cluster_cidr = cfg.network.cluster.cidr
        off_l2 = sorted(
            name
            for name, group in cfg.metal.groups.items()
            if group.network.cidr != cluster_cidr
        )
        off_l2 += sorted(
            f"{group.name}/{server.name}"
            for group in cfg.metal.groups.values()
            for server in group.servers.values()
            # a server still on the group's L2 is covered by the group's own
            # entry above
            if server.network.cidr not in (group.network.cidr, cluster_cidr)
        )
        if off_l2:
            raise ConfigError(
                "cluster.yaml: talos.kubespan must be true when a metal group's "
                "or a server's network differs from network.cluster "
                f"({', '.join(off_l2)}): "
                "the KubeSpan overlay is what carries their traffic to the cluster"
            )

    if "controlplane" in cfg.workers:
        raise ConfigError("worker pool name 'controlplane' is reserved")

    pools = {"controlplane": cfg.controlplane, **cfg.workers}
    for pool_name, p in pools.items():
        if not isinstance(pool_name, str) or not _NAME_RE.fullmatch(pool_name):
            raise ConfigError(f"worker pool name {pool_name!r} is not a valid hostname component")
        if not isinstance(p, dict):
            raise ConfigError(f"pool '{pool_name}' must be a YAML mapping")
        if isinstance(cfg.provider, OpenStackConfig):
            required: tuple[str, ...] = ("count", "flavor", "disk")
        else:
            required = ("count", "cores", "memory", "disk")
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
        elif isinstance(cfg.provider, ProxmoxConfig):
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

    # already validated as an IPv4 network when the block was parsed
    network = ipaddress.ip_network(cfg.network.cluster.cidr, strict=True)

    provider_fields: tuple[tuple[str, str], ...] = ()
    if isinstance(cfg.provider, OpenStackConfig):
        provider_fields = (
            ("openstack.url", cfg.provider.url),
            ("openstack.availability_zone", cfg.provider.availability_zone),
            ("openstack.external_net", cfg.provider.external_net),
        )
    elif isinstance(cfg.provider, ProxmoxConfig):
        provider_fields = (("proxmox.url", cfg.provider.url),)
    for field_name, value in provider_fields:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"cluster.yaml: {field_name} must be a non-empty string")
    if isinstance(cfg.provider, OpenStackConfig):
        # OpenStack builds its own external connectivity at converge, so the
        # keys that describe one by hand would be silently ignored here
        if cfg.network.external is not None:
            raise ConfigError(
                "cluster.yaml: network.external is not valid with openstack: OpenStack "
                "allocates the external network itself from openstack.external_net "
                "(a router, and floating IPs for the API and ingress ports)"
            )
        if cfg.network.cluster.kubeapi_vip:
            raise ConfigError(
                "cluster.yaml: network.cluster.kubeapi_vip is not valid with openstack: "
                "converge reserves the API address as a port on the tenant network"
            )
        if cfg.network.cluster.vlan is not None:
            raise ConfigError(
                "cluster.yaml: network.cluster.vlan is not valid with openstack: the VLAN "
                "tag is the Proxmox VM NIC setting; the tenant network carries no tag"
            )
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
        _reject_unknown_keys(
            cluster_network, "cluster.yaml: proxmox.network.cluster", _PROXMOX_CLUSTER_KEYS
        )
        links = [name for name in ("bridge", "vnet") if cluster_network.get(name)]
        if "sdn" in cluster_network:
            if links or cfg.network.cluster.vlan is not None:
                raise ConfigError(
                    "cluster.yaml: proxmox.network.cluster.sdn is mutually exclusive "
                    "with bridge, vnet, and network.cluster.vlan"
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
        external_network = _mapping(
            provider.network.get("external"), "cluster.yaml: proxmox.network.external"
        )
        _reject_unknown_keys(
            external_network, "cluster.yaml: proxmox.network.external", _PROXMOX_EXTERNAL_KEYS
        )
        # `network.external` describes the subnet, `proxmox.network.external`
        # the bridge that reaches it: neither is usable without the other
        bridge = external_network.get("bridge")
        if cfg.network.external is not None and (
            not isinstance(bridge, str) or not bridge.strip()
        ):
            raise ConfigError(
                "cluster.yaml: proxmox.network.external.bridge must be a non-empty string"
            )
        if external_network and cfg.network.external is None:
            raise ConfigError(
                "cluster.yaml: proxmox.network.external needs a network.external block "
                "describing the subnet on that bridge"
            )
        cluster_vip = cfg.network.cluster.kubeapi_vip
        external_vip = cfg.network.external.kubeapi_vip if cfg.network.external else ""
        if not cluster_vip and not external_vip:
            raise ConfigError(
                "cluster.yaml: kubeapi_vip must be set in network.cluster or network.external"
            )
        if cluster_vip and _ipv4_address(
            cluster_vip, "network.cluster.kubeapi_vip"
        ) not in network:
            raise ConfigError(
                "cluster.yaml: network.cluster.kubeapi_vip must be inside network.cluster.cidr"
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
