"""Provider-neutral infrastructure boundary used by shared cluster orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import DEFAULT_MTU, Config, Machine, OpenStackConfig, ProxmoxConfig


@dataclass(frozen=True)
class Endpoint:
    """An address Talos owns and the address clients use for that endpoint."""

    vip: str = ""
    advertised_address: str = ""


@dataclass(frozen=True)
class NetworkAttachment:
    name: str
    address: str = ""


@dataclass(frozen=True)
class InfrastructureMachine:
    name: str
    attachments: tuple[NetworkAttachment, ...] = ()

    @property
    def address(self) -> str:
        return next(
            (attachment.address for attachment in self.attachments if attachment.address), ""
        )


@dataclass
class InfrastructureInventory:
    machines: dict[str, InfrastructureMachine] = field(default_factory=dict)
    resources: dict[str, list[str]] = field(default_factory=dict)
    provider_data: Any = field(default=None, repr=False)

    def machine_address(self, name: str) -> str:
        machine = self.machines.get(name)
        return machine.address if machine else ""


@dataclass(frozen=True)
class NetworkResult:
    kubernetes: Endpoint = Endpoint()
    ingress: Endpoint = Endpoint()
    #: MetalLB address pool handed to ArgoCD: single VIPs (OpenStack) or the
    #: ``ingress_pool`` range (Proxmox). Empty when the provider does not expose
    #: addresses to allocate.
    metallb: tuple[str, ...] = ()
    machine_attachments: dict[str, tuple[NetworkAttachment, ...]] = field(default_factory=dict)

    def machine_address(self, name: str) -> str:
        return next(
            (
                attachment.address
                for attachment in self.machine_attachments.get(name, ())
                if attachment.address
            ),
            "",
        )


@dataclass(frozen=True)
class TalosPatch:
    """One named machine-config patch or Talos resource document.

    `document` is either a patch mapping, a list of Talos resource documents, or
    a raw YAML string. The shared generator writes it to `<host>-<name>.yaml`
    and stacks it as a `--config-patch`; it never inspects the content.
    """

    name: str
    document: dict[str, Any] | list[dict[str, Any]] | str


def stated_mtu(mtu: int) -> int | None:
    """The MTU a LinkConfig states for its layer-2 network, or None at the default.

    MTU is written into the machine configuration only when an L2 is jumbo: at
    1500 the field would just repeat Talos's own default, so a default cluster
    keeps the documents it always had.
    """
    return mtu if mtu > DEFAULT_MTU else None


def dhcp_link_documents(
    link: str, vip: str | None = None, mtu: int = DEFAULT_MTU, gateway: str = ""
) -> list[dict[str, Any]]:
    """LinkConfig + DHCPv4Config for one physical link, plus its Layer 2 VIP.

    Any new-style link document turns off Talos's default DHCP on physical
    links, so the lease the node used to get implicitly is requested
    explicitly. `vip` adds a Layer2VIPConfig on the same link (control planes).
    `mtu` is the layer-2 network's MTU, stated on the link only when it is
    above the default. On a jumbo L2 a known `gateway` restates the default
    route with an MTU of 1500: the configuration's route replaces the route
    the lease provides, so off-subnet traffic stays clamped.
    """
    link_doc: dict[str, Any] = {"apiVersion": "v1alpha1", "kind": "LinkConfig", "name": link}
    stated = stated_mtu(mtu)
    if stated is not None:
        link_doc["mtu"] = stated
        if gateway:
            link_doc["routes"] = [{"gateway": gateway, "mtu": DEFAULT_MTU}]
    docs: list[dict[str, Any]] = [
        link_doc,
        {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": link},
    ]
    if vip:
        docs.append(
            {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": vip, "link": link}
        )
    return docs


@dataclass(frozen=True)
class TalosContribution:
    """Everything a provider adds to one machine's Talos configuration."""

    install_disk: str
    patches: tuple[TalosPatch, ...] = ()


class InfrastructureBackend(Protocol):
    name: str
    # Talos Image Factory installer platform for this provider's boot artifacts.
    installer_platform: str
    # True when the provider's machines boot the SecureBoot ISO and install the
    # SecureBoot (UKI) installer variant.
    installer_secureboot: bool

    def talos_contribution(
        self, machine: Machine, endpoint: Endpoint
    ) -> TalosContribution: ...

    def load_inventory(self) -> InfrastructureInventory: ...

    def ensure_boot_artifact(self) -> str: ...

    def reconcile_network(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> NetworkResult: ...

    def current_network(self, inventory: InfrastructureInventory) -> NetworkResult: ...

    def validate_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> None:
        """Refuse unsupported machine changes before any converge mutation.

        Providers that cannot reconcile every change in place (e.g. an OpenStack
        flavor, disk or availability-zone change, a Proxmox disk shrink, placement
        or storage change, or a NIC attachment move) reject them here, ahead of the
        image, network and Talos phases. Providers without such a preflight may
        no-op.
        """
        ...

    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> set[str]:
        """Create missing machines and reconcile existing ones; returns the names
        of machines whose applied changes only take effect after a restart."""
        ...

    def restart_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        """Restart one machine through the provider so it picks up applied
        sizing changes; returns once the provider reports it started again."""
        ...

    def finalize_machines(self, inventory: InfrastructureInventory) -> None: ...

    def delete_machine(self, name: str, inventory: InfrastructureInventory) -> None: ...

    def default_node_tags(self) -> dict[str, str]: ...

    def provider_status(self) -> dict[str, Any]: ...

    def print_environment(self) -> None: ...

    def download_image(self) -> str: ...

    def remove_image(self, assume_yes: bool = False) -> None: ...

    def destroy_summary(self, inventory: InfrastructureInventory) -> str: ...

    def destroy_resources(self, inventory: InfrastructureInventory) -> None: ...


def backend_for(cfg: Config) -> InfrastructureBackend:
    if isinstance(cfg.provider, OpenStackConfig):
        from .openstack.backend import OpenStackBackend

        return OpenStackBackend(cfg)
    if isinstance(cfg.provider, ProxmoxConfig):
        from .proxmox.backend import ProxmoxBackend

        return ProxmoxBackend(cfg)
    raise TypeError(f"unsupported infrastructure provider: {type(cfg.provider).__name__}")


def resolve_node_address(
    name: str,
    discovered: dict[str, str],
    inventory: InfrastructureInventory,
    network: NetworkResult | None = None,
) -> str:
    """Prefer Talos discovery, then provider network results and inventory."""
    return (
        discovered.get(name, "")
        or (network.machine_address(name) if network is not None else "")
        or inventory.machine_address(name)
    )
