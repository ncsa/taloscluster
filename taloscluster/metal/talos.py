"""The metal provider's machine configuration.

The cabling plan (`metal.<group>.interfaces`) drives one machine's network
configuration: every link gets a classic ``machine.network.interfaces`` device
entry that keeps DHCP off -- the boot link gets nothing else -- while new-style
link documents create an ``external`` link's VLAN child over the parent port
(the ``VLANConfig`` names it, so a ``link_name`` override names the link Talos
actually creates) and carry the static addresses, the default route, the MTUs
and the external network's policy routing. The full config is generated through
the same ``talosctl gen config`` pipeline as every other node's.

A machine with an ``external`` link also runs the return-path static pod: it
marks the connections entering the VLAN child so their replies leave through
the external gateway instead of the default route -- MetalLB ingress on the
machine would otherwise answer from the wrong interface.

The hostname rides the same ``HostnameConfig`` document every node's
configuration carries -- Talos has accepted it, and
``machine.install.grubUseUKICmdline``, since 1.12, older than the minimum
supported ``talos.version`` -- so the generated config is used as the client
emits it, following the document layout of the ``--talos-version`` it names
(v1alpha1 fields up to 1.13, typed documents from 1.14 on). A metal machine is
configured in maintenance mode where the running Talos matches the version its
install media was built for.
"""

from __future__ import annotations

import ipaddress
import tempfile
from pathlib import Path

import yaml

from ..config import (
    DEFAULT_MTU,
    Config,
    ConfigError,
    Machine,
    MetalInterface,
    MetalServer,
)
from ..infrastructure import Endpoint, stated_mtu
from ..naming import METAL_BASE_EXTENSIONS
from ..proxmox.talos import (
    EXT_RETURN_MARK,
    EXT_RETURN_RULE_PRIORITY,
    EXT_RETURN_TABLE,
    EXT_ROUTE_TABLE,
    anchor_address,
)
from ..talos import factory, machineconfig, talosctl


def cluster_ip(server: MetalServer) -> str:
    """The static address on the machine's cluster link, where apid answers.

    A metal machine belongs to no provider inventory and reports no guest
    agent, so this address is the only way to reach it -- converge pushes the
    machine's configuration and its Talos upgrades here. The loader refuses a
    machine whose cluster link has no static address.
    """
    ifname = next(n for n, i in server.interfaces.items() if "cluster" in i.role)
    return server.interfaces[ifname].ip.split("/", 1)[0]


def answers_as_cluster(talosconfig: Path, server: MetalServer,
                       endpoint: str) -> bool | None:
    """Whether the machine already answers apid with this cluster's identity.

    A machine running a configuration refuses the insecure maintenance API but
    answers the cluster's apid -- it is joined, and driving it through the
    install media again would wipe it. `metal join` refuses such a machine and
    converge's compute phase skips it; both probe here so the two flows can
    never drift apart.

    The cluster probe dials through `endpoint`, the control plane every other
    converge call goes through, because this host may not route the machine's
    address directly; the maintenance probe must dial the machine itself, the
    insecure API having no cluster credentials to proxy with. None when neither
    apid answers: the machine may be down, mid-boot, or unreachable even from
    the control plane, and an undecided probe must never read as "not joined"
    to a caller that would force-restart it into the install media.
    """
    ip = cluster_ip(server)
    if talosctl.maintenance_reachable(ip):
        return False
    if not endpoint:
        return None
    if not talosctl.reachable(talosconfig, endpoint=endpoint, node=ip):
        return None
    return True


def installer(cfg: Config) -> tuple[str, str]:
    """(schematic id, installer image ref) for the cluster's metal machines.

    Metal machines belong to no VM pool, so the resolved extension set is the
    one `Config.metal_extensions` resolves -- the base extensions (tailscale
    only when configured, and never the VM-only ones -- bare metal has no QEMU
    host for qemu-guest-agent to reach) plus the cluster-wide and group-level
    ones -- and the installer reference rides the metal platform.
    """
    schematic = factory.schematic_id(cfg.metal_extensions())
    return schematic, factory.installer_image(schematic, cfg.talos_version, platform="metal")


def iso_url(cfg: Config) -> str:
    """The factory install ISO a metal machine boots, metal base set baked in.

    No qemu-guest-agent: its service never starts on bare metal and would
    leave the machine blocked in `startAllServices` short of the maintenance
    apid. The asset is the shared nocloud ISO, not a metal-platform one --
    the boot media only has to reach maintenance mode, and the platform the
    machine installs and runs comes from the installer `installer` resolves --
    so metal boots the nocloud ISO yet installs the metal installer by design.
    """
    return factory.nocloud_iso_url(
        factory.schematic_id(METAL_BASE_EXTENSIONS), cfg.talos_version
    )


def _machine(server: MetalServer, cfg: Config) -> Machine:
    """The server as the shared patch builders see it: pool is the group."""
    return Machine(
        name=server.name,
        role=server.role,
        pool=server.group,
        disk=0,
        extensions=(),
        config_patches=tuple(cfg.talos_config_patches),
        tags=dict(cfg.tags),
    )


def _machine_patch(server: MetalServer, cfg: Config,
                   default_tags: dict[str, str] | None = None,
                   talos_version: str | None = None) -> dict | list[dict]:
    m = _machine(server, cfg)
    # a group on its own L2 pins the pod node IP to that L2, not the cluster's
    return machineconfig._machine_patch(
        m, cfg, server.disk, default_tags,
        node_cidr=server.network.cidr, talos_version=talos_version,
    )


def _with_prefix(address: str, cidr: str) -> str:
    """A link address needs CIDR notation; a bare address takes the L2's prefix."""
    if "/" in address:
        return address
    return f"{address}/{ipaddress.ip_network(cidr).prefixlen}"


def _external_child(server: MetalServer, cfg: Config, ifname: str,
                    iface: MetalInterface) -> tuple[int, str]:
    """The VLAN id and link name of an external interface's VLAN child."""
    ext = cfg.network.external
    assert ext is not None  # the loader refuses the external role without a block
    vlan = iface.vlan if iface.vlan is not None else ext.vlan
    if vlan is None:
        raise ConfigError(
            f"metal server {server.name}: interface {ifname} needs a VLAN id: "
            "set network.external.vlan or the interface's vlan"
        )
    return vlan, iface.link_name or f"{ifname}.{vlan}"


def network_docs(server: MetalServer, cfg: Config, vip: str = "") -> list[dict]:
    """The new-style link documents for one machine's cabling plan.

    The `cluster` link states its static address and the default route via the
    group L2's gateway (MTU-clamped on a jumbo L2); an `external` link's VLAN
    child carries the anchor address from `network.external.anchor_cidr`, a
    static address of its own when the interface has no cluster role, and the
    routes to the external network over the return-path routing table. A
    control plane states the kubeapi VIP `vip` on the link that carries it.
    """
    docs, _ = _cabling(server, cfg, vip)
    return docs


def device_entries(server: MetalServer, cfg: Config) -> list[dict]:
    """The classic `machine.network.interfaces` device entries for one machine.

    Every link states `dhcp: false` -- static links and the boot link alike
    must never pick up a lease. The `external` link's VLAN child is created
    and configured by the link documents, so the entries carry nothing
    VLAN-specific.
    """
    _, entries = _cabling(server, cfg, "")
    return entries


def _cabling(server: MetalServer, cfg: Config, vip: str = "") -> tuple[list[dict], list[dict]]:
    """(new-style link documents, classic device entries) for one machine.

    The loader has checked the cabling plan
    (:func:`~taloscluster.config._check_metal_cabling`), so exactly one link
    carries the cluster role and an `external` link implies a
    `network.external` block. A control plane whose kubeapi_vip rides
    `network.external` but has no `external` link could never hold the VIP,
    so that is refused here. `vip` is the cluster endpoint address the
    provider resolved -- the one the VM nodes' configurations carry -- and a
    control plane is refused without one rather than left unable to hold the
    API address.
    """
    interfaces = server.interfaces
    ext = cfg.network.external
    docs: list[dict] = []
    entries: list[dict] = []
    cluster_ifname = next(
        n for n, i in interfaces.items() if "cluster" in i.role
    )
    child_name = ""
    for ifname in sorted(interfaces):
        iface = interfaces[ifname]
        entry: dict = {"interface": ifname, "dhcp": False}
        if "cluster" in iface.role:
            link: dict = {
                "apiVersion": "v1alpha1",
                "kind": "LinkConfig",
                "name": ifname,
            }
            stated = stated_mtu(server.network.mtu)
            if stated is not None:
                link["mtu"] = stated
            if iface.ip:
                link["addresses"] = [
                    {"address": _with_prefix(iface.ip, server.network.cidr)}
                ]
            if server.network.gateway:
                route: dict = {"gateway": server.network.gateway}
                if stated is not None:
                    # a gateway that drops jumbo frames sends no ICMP, so the
                    # route MTU keeps off-subnet traffic working
                    route["mtu"] = DEFAULT_MTU
                link["routes"] = [route]
            docs.append(link)
        if "external" in iface.role:
            assert ext is not None
            vlan, child_name = _external_child(server, cfg, ifname, iface)
            if "cluster" not in iface.role:
                # a dedicated external NIC states its own L2's MTU; a VLAN child
                # can never exceed the port it rides on
                parent: dict = {
                    "apiVersion": "v1alpha1",
                    "kind": "LinkConfig",
                    "name": ifname,
                }
                stated = stated_mtu(ext.mtu)
                if stated is not None:
                    parent["mtu"] = stated
                docs.append(parent)
            # the VLANConfig creates the child named `child_name` -- the classic
            # `vlans` entry would name it `<parent>.<vlanId>`, so a `link_name`
            # override would describe a link that never exists
            child: dict = {
                "apiVersion": "v1alpha1",
                "kind": "VLANConfig",
                "name": child_name,
                "vlanID": vlan,
                "parent": ifname,
            }
            addresses = []
            if ext.anchor_cidr:
                addresses.append(
                    {"address": anchor_address(ext.anchor_cidr, cfg.name, server.name)}
                )
            if iface.ip and "cluster" not in iface.role:
                # a dedicated external NIC: its own address rides the child
                addresses.append({"address": _with_prefix(iface.ip, ext.cidr)})
            if addresses:
                child["addresses"] = addresses
            routes = []
            if ext.cidr:
                routes.append({"destination": ext.cidr, "table": EXT_ROUTE_TABLE})
            if ext.gateway:
                route = {"gateway": ext.gateway, "table": EXT_ROUTE_TABLE}
                if stated_mtu(ext.mtu) is not None:
                    # a gateway that drops jumbo frames sends no ICMP, so the
                    # route MTU keeps the replies this table routes working
                    # off-subnet, like the cluster link's clamped default route
                    route["mtu"] = DEFAULT_MTU
                routes.append(route)
            if routes:
                child["routes"] = routes
            # the child inherits the parent port's MTU; state the external L2's
            # own whenever the two differ so a jumbo parent never drags it up
            parent_mtu = server.network.mtu if "cluster" in iface.role else DEFAULT_MTU
            if ext.mtu != parent_mtu:
                child["mtu"] = ext.mtu
            docs.append(child)
        entries.append(entry)

    if child_name:
        # externally initiated connections are marked so their replies return
        # through the external gateway; the rule sends marked traffic to table 100
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "RoutingRuleConfig",
                "name": EXT_RETURN_RULE_PRIORITY,
                "fwMark": EXT_RETURN_MARK,
                "fwMask": EXT_RETURN_MARK,
                "table": EXT_ROUTE_TABLE,
            }
        )
    if server.role == "controlplane":
        if not vip:
            raise ConfigError(
                f"metal server {server.name}: the provider resolved no kubeapi "
                "VIP for the control plane to hold"
            )
        on_external = bool(cfg.network.external and cfg.network.external.kubeapi_vip)
        if on_external and not child_name:
            raise ConfigError(
                f"metal server {server.name}: kubeapi_vip rides network.external, "
                "but the server has no external link"
            )
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "Layer2VIPConfig",
                "name": vip,
                "link": child_name if on_external else cluster_ifname,
            }
        )
    dns = next(
        (i.dns for i in (interfaces[n] for n in sorted(interfaces)) if i.dns), None
    ) or tuple(cfg.network.dns)
    if dns:
        docs.append(
            {
                "apiVersion": "v1alpha1",
                "kind": "ResolverConfig",
                "nameservers": [{"address": d} for d in dns],
            }
        )
    return docs, entries


def _external_child_link(server: MetalServer, cfg: Config) -> str:
    """The VLAN child link name of the machine's external link, or none."""
    external = [n for n, i in server.interfaces.items() if "external" in i.role]
    if not external:
        return ""
    return _external_child(server, cfg, external[0], server.interfaces[external[0]])[1]


def return_path_pod(server: MetalServer, cfg: Config, child: str,
                    kubernetes_version: str | None = None) -> dict:
    """The static pod marking the connections entering the machine's VLAN child.

    The same marking the Proxmox return-path pod installs, matched on the
    child link's stable name instead of a generated MAC: replies to
    externally initiated connections are marked so the fwmark routing rule
    sends them back through the external gateway, not the default route.
    `kubernetes_version` retags the kube-proxy image to the cluster's RUNNING
    version, the same override `build_configs` applies for the VM providers.
    """
    ext = cfg.network.external
    assert ext is not None  # an external link implies a network.external block
    mark = f"0x{EXT_RETURN_MARK:08x}"
    ingress_rule = (
        f'iifname "{child}" ip daddr {ext.cidr} '
        f"ct direction original ct mark set ct mark | {mark}"
    )
    script = f"""\
set -eu
NFT=nft

cleanup() {{
  "$NFT" delete table ip {EXT_RETURN_TABLE} 2>/dev/null || true
}}
trap cleanup EXIT
cleanup

"$NFT" -f - <<EOF
table ip {EXT_RETURN_TABLE} {{
  chain prerouting {{
    type filter hook prerouting priority -160; policy accept;
    {ingress_rule}
    ct direction reply ct mark & {mark} != 0 meta mark set meta mark | {mark}
  }}
}}
EOF

while "$NFT" list table ip {EXT_RETURN_TABLE} >/dev/null 2>&1; do
  sleep 30
done
exit 1
"""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "taloscluster-metal-return-path",
            "namespace": "kube-system",
        },
        "spec": {
            "hostNetwork": True,
            "priorityClassName": "system-node-critical",
            "restartPolicy": "Always",
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "return-path",
                    # kube-proxy ships nft (it runs in nftables mode) and is
                    # already present on every node; Talos has no host nft
                    # visible to the kubelet, so hostPath mounts can't work.
                    # The version below is the TARGET; build_config passes the
                    # cluster's RUNNING version once a kubeconfig exists, so a
                    # machine joined after a version bump never pulls the
                    # target kube-proxy before the minor-by-minor upgrade.
                    "image": f"registry.k8s.io/kube-proxy:"
                             f"{kubernetes_version or cfg.kubernetes_version}",
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-ec", script],
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {
                            "drop": ["ALL"],
                            "add": ["NET_ADMIN"],
                        },
                    },
                    "resources": {
                        "requests": {"cpu": "5m", "memory": "8Mi"},
                        "limits": {"memory": "32Mi"},
                    },
                }
            ],
        },
    }


def build_config(
    server: MetalServer,
    cfg: Config,
    secrets_path: Path,
    installer_image: str,
    endpoint: Endpoint,
    default_tags: dict[str, str] | None = None,
    kubernetes_version: str | None = None,
    talos_version: str | None = None,
) -> str:
    """Return one metal machine's machine-config YAML string.

    The shared patch stack (machine, hostname, cluster, firewall, kubespan,
    tailscale) is assembled exactly as `build_configs` does for the VM
    providers -- the firewall and the control plane's etcd advertisement keyed
    on     the machine's own L2 -- then the cabling plan's network patches and the
    cluster's freeform patches. `default_tags` are
    the provider's default node labels (`ncsa/project` on OpenStack), merged
    under the machine's `tags:` exactly as `build_configs` merges them for the
    VM machines. `endpoint` is the
    provider-resolved cluster endpoint the VM machines' configurations carry:
    its advertised address names the endpoint in the generated config and the
    certSANs, and its vip is what a control plane holds as a Layer 2 VIP on
    the link that carries it. `kubernetes_version`
    overrides `cfg.kubernetes_version` for the kubelet and control-plane images
    and the return-path pod's kube-proxy image: `metal apply` passes the
    running cluster's version so a machine joined after a `kubernetes.version`
    bump never starts newer than the API server -- the target is only for a
    cluster that has never been bootstrapped. `talos_version` overrides the
    document layout the same way (the version a joined machine RUNS decides
    the layout its config push is generated in); a machine being configured in
    maintenance mode runs its install media, so the default target is right
    for it.
    """
    kubernetes = kubernetes_version or cfg.kubernetes_version
    talos = talos_version or cfg.talos_version
    host = server.name
    m = _machine(server, cfg)
    # the cluster's LUKS2 passphrase, when the secrets carry one: a machine
    # this installs encrypts STATE and EPHEMERAL (see machineconfig)
    passphrase = machineconfig.disk_passphrase(secrets_path)
    with tempfile.TemporaryDirectory(prefix="taloscluster-metal-mc-") as tmp:
        workdir = Path(tmp)
        patches = [
            machineconfig._write(
                workdir, f"{host}-machine",
                _machine_patch(server, cfg, default_tags, talos_version=talos),
            ),
            machineconfig._write(
                workdir, f"{host}-hostname", machineconfig._hostname_patch(m)
            ),
        ]
        if passphrase:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-encryption",
                    machineconfig._disk_encryption_patch(passphrase),
                )
            )
        if server.role == "controlplane":
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-cluster",
                    machineconfig._cluster_patch(
                        cfg, node_cidr=server.network.cidr, talos_version=talos,
                    ),
                )
            )
        patches.append(
            machineconfig._write(
                workdir, f"{host}-firewall",
                machineconfig._firewall_docs(cfg, node_cidr=server.network.cidr),
            )
        )
        if cfg.kubespan:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-kubespan",
                    # the WireGuard MTU follows the machine's own L2, which a
                    # group on another network carries with its own mtu
                    machineconfig._kubespan_patch(cfg, mtu=server.network.mtu),
                )
            )
        # the patch rides the same predicate `build_configs` applies to the VM
        # machines: the key is set and the resolved extensions carry tailscale
        # (on metal, the set the installer bakes, which honours an explicit
        # `talos.extensions` or group `extensions` entry even without a
        # `tailscale:` section; the section may live in any merged file,
        # secrets.yaml included)
        extensions = cfg.metal_extensions()
        auth_key = cfg.tailscale_auth_key
        if auth_key and "siderolabs/tailscale" in extensions:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-tailscale",
                    machineconfig._tailscale_patch(m, cfg, auth_key),
                )
            )
        docs, entries = _cabling(server, cfg, vip=endpoint.vip)
        patches.append(machineconfig._write(workdir, f"{host}-network", docs))
        if entries:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-interfaces",
                    {"machine": {"network": {"interfaces": entries}}},
                )
            )
        child = _external_child_link(server, cfg)
        if child:
            patches.append(
                machineconfig._write(
                    workdir, f"{host}-return-path",
                    {"machine": {"pods": [
                        return_path_pod(server, cfg, child, kubernetes)
                    ]}},
                )
            )
        for i, raw in enumerate(m.config_patches):
            patches.append(machineconfig._write(workdir, f"{host}-extra-{i}", raw))

        raw = talosctl.gen_config(
            cluster=cfg.name,
            endpoint=f"https://{endpoint.advertised_address}:6443",
            secrets_path=secrets_path,
            output_type="controlplane" if server.role == "controlplane" else "worker",
            install_image=installer_image,
            install_disk=server.disk,
            kubernetes_version=kubernetes,
            talos_version=talos,
            patches=patches,
            additional_sans=[endpoint.advertised_address],
        )
    generated = [doc for doc in yaml.safe_load_all(raw) if doc]
    return yaml.safe_dump_all(generated, sort_keys=False, explicit_start=True)
