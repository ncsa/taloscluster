"""Provider-neutral infrastructure boundary used by shared cluster orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Config, Machine, OpenStackConfig, ProxmoxConfig, Secrets


@dataclass(frozen=True)
class Endpoint:
    """An address Talos owns and the address clients use for that endpoint."""

    vip: str = ""
    advertised_address: str = ""


@dataclass(frozen=True)
class NetworkAttachment:
    name: str
    address: str = ""
    provider_id: str = ""


@dataclass(frozen=True)
class InfrastructureMachine:
    name: str
    provider_id: str = ""
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


@dataclass(frozen=True)
class TalosContribution:
    """Everything a provider adds to one machine's Talos configuration."""

    install_disk: str
    patches: tuple[TalosPatch, ...] = ()


class InfrastructureBackend(Protocol):
    name: str
    # Talos Image Factory installer platform for this provider's boot artifacts.
    installer_platform: str

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

    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> None: ...

    def finalize_machines(self, inventory: InfrastructureInventory) -> None: ...

    def delete_machine(self, name: str, inventory: InfrastructureInventory) -> None: ...

    def default_node_tags(self) -> dict[str, str]: ...

    def provider_status(self) -> dict[str, str]: ...

    def print_environment(self) -> None: ...

    def download_image(self) -> str: ...

    def remove_image(self, assume_yes: bool = False) -> None: ...

    def destroy_summary(self, inventory: InfrastructureInventory) -> str: ...

    def destroy_resources(self, inventory: InfrastructureInventory) -> None: ...


def backend_for(cfg: Config, secrets: Secrets) -> InfrastructureBackend:
    if isinstance(cfg.provider, OpenStackConfig):
        from .openstack.backend import OpenStackBackend

        return OpenStackBackend(cfg, secrets)
    if isinstance(cfg.provider, ProxmoxConfig):
        from .proxmox.backend import ProxmoxBackend

        return ProxmoxBackend(cfg, secrets)
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
