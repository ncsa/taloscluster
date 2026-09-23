"""OpenStack implementation of the provider-neutral infrastructure boundary."""

from __future__ import annotations

import functools
import shlex

from keystoneauth1.exceptions import ClientException, RetriableConnectionFailure

from openstack import exceptions as os_exceptions

from .. import naming
from ..config import Config, Machine, OpenStackConfig
from ..errors import ConfigError, ReconcileError
from ..infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
    TalosContribution,
)
from ..output import action, dry_run, info, log, warn
from ..talos import factory
from . import compute, image, network, security, talos
from .network import _fixed_ip
from .session import Inventory, connect, project_name

_STATUS_KINDS = (
    "networks", "subnets", "routers", "security_groups", "ports", "ips", "servers",
)


def _reconcile_errors(fn):
    """Translate an uncaught provider SDK error into a clean ReconcileError.

    Mirrors the Proxmox client, which turns every API error into a
    ``ReconcileError`` at the client boundary, so ``cli.main`` prints a
    one-line ``ERROR:`` and exits 1 instead of leaking a traceback. Both the
    ``openstack.exceptions.SDKException`` tree (a Neutron 409, a
    ``wait_for_delete`` timeout, a quota error) and the keystoneauth1 tree
    (a rejected application credential raising ``Unauthorized``, an
    unreachable or timing-out cloud raising ``ConnectFailure``/``ConnectTimeout``)
    are wrapped here. Nested helpers that catch a specific SDK case themselves
    (``tags.create_tagged`` catches only ``BadRequestException`` for the legacy
    tags-in-post fallback and re-raises everything else) are unaffected: only an
    error that escapes the whole backend method is wrapped here.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (
            os_exceptions.SDKException,
            ClientException,
            RetriableConnectionFailure,
        ) as exc:
            raise ReconcileError(f"OpenStack API error: {exc}") from exc
    return wrapper


class OpenStackBackend:
    name = "openstack"
    installer_platform = talos.INSTALLER_PLATFORM
    # the disk image boots through Nova, which offers no Secure Boot enrollment
    installer_secureboot = False

    def __init__(self, cfg: Config):
        if not isinstance(cfg.provider, OpenStackConfig):
            raise ConfigError("OpenStack backend requires openstack configuration")
        self.cfg = cfg
        self.conn = connect(cfg)

    def talos_contribution(
        self, machine: Machine, endpoint: Endpoint
    ) -> TalosContribution:
        return talos.contribution(machine, self.cfg, endpoint)

    @_reconcile_errors
    def load_inventory(self) -> InfrastructureInventory:
        raw = Inventory(self.conn, self.cfg.name).load()
        machines: dict[str, InfrastructureMachine] = {}
        for name in raw.all("servers"):
            port = raw.get("ports", name)
            attachment = NetworkAttachment(
                name=name,
                address=_fixed_ip(port),
            )
            machines[name] = InfrastructureMachine(
                name=name,
                attachments=(attachment,),
            )
        resources = {kind: sorted(raw.all(kind)) for kind in _STATUS_KINDS}
        return InfrastructureInventory(
            machines=machines,
            resources=resources,
            provider_data=raw,
        )

    @staticmethod
    def _raw(inventory: InfrastructureInventory) -> Inventory:
        raw = inventory.provider_data
        if not isinstance(raw, Inventory):
            raise ReconcileError("OpenStack inventory is unavailable")
        return raw

    @_reconcile_errors
    def ensure_boot_artifact(self) -> str:
        return image.ensure_image(self.conn, self.cfg)

    @_reconcile_errors
    def reconcile_network(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> NetworkResult:
        raw = self._raw(inventory)
        sg = security.reconcile(self.conn, self.cfg, raw)
        refs = network.reconcile(self.conn, self.cfg, machines, raw, sg)
        attachments: dict[str, tuple[NetworkAttachment, ...]] = {
            name: (NetworkAttachment(name=name, address=address),)
            for name, address in refs.machine_private_ips.items()
        }
        return NetworkResult(
            kubernetes=Endpoint(
                vip=refs.kubeapi_vip,
                advertised_address=refs.kubeapi_fip,
            ),
            ingress=Endpoint(
                vip=refs.ingress_vip,
                advertised_address=refs.ingress_fip,
            ),
            metallb=(refs.ingress_vip,) if refs.ingress_vip else (),
            machine_attachments=attachments,
        )

    def current_network(self, inventory: InfrastructureInventory) -> NetworkResult:
        raw = self._raw(inventory)

        def endpoint(name: str) -> Endpoint:
            floating_ip = raw.get("ips", name)
            return Endpoint(
                vip=_fixed_ip(raw.get("ports", name)),
                advertised_address=(
                    getattr(floating_ip, "floating_ip_address", "") or ""
                    if floating_ip is not None
                    else ""
                ),
            )

        ingress_name = naming.ingress_name(self.cfg.name)
        ingress_port = raw.get("ports", ingress_name)
        return NetworkResult(
            kubernetes=endpoint(naming.kubeapi_name(self.cfg.name)),
            ingress=endpoint(ingress_name),
            metallb=(_fixed_ip(ingress_port),) if ingress_port else (),
        )

    @_reconcile_errors
    def validate_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
    ) -> None:
        compute.validate(
            self.conn,
            self.cfg,
            machines,
            self._raw(inventory),
        )

    @_reconcile_errors
    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> set[str]:
        compute.reconcile(
            self.conn,
            self.cfg,
            machines,
            self._raw(inventory),
            boot_artifact,
            configs,
        )
        return set()

    @_reconcile_errors
    def delete_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        compute.delete_node(self.conn, name, self._raw(inventory))

    @_reconcile_errors
    def restart_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        compute.restart_node(self.conn, name, self._raw(inventory))

    def finalize_machines(self, inventory: InfrastructureInventory) -> None:
        return None

    @_reconcile_errors
    def default_node_tags(self) -> dict[str, str]:
        project = project_name(self.conn)
        return {"ncsa/project": project} if project else {}

    @_reconcile_errors
    def provider_status(self) -> dict[str, str]:
        return {
            "url": self.cfg.openstack_url,
            "region": self.cfg.region,
            "project": project_name(self.conn),
        }

    def print_environment(self) -> None:
        print(f"export OS_AUTH_URL={shlex.quote(self.cfg.openstack_url)}")
        print("export OS_AUTH_TYPE=v3applicationcredential")
        print(f"export OS_REGION_NAME={shlex.quote(self.cfg.region)}")
        credential_id, credential_secret = self.cfg.openstack_credentials
        print(f"export OS_APPLICATION_CREDENTIAL_ID={shlex.quote(credential_id)}")
        print(
            f"export OS_APPLICATION_CREDENTIAL_SECRET={shlex.quote(credential_secret)}"
        )

    @_reconcile_errors
    def download_image(self) -> str:
        return image.ensure_image(self.conn, self.cfg)

    @_reconcile_errors
    def remove_image(self, assume_yes: bool = False) -> None:
        schematic = factory.schematic_id(naming.BASE_EXTENSIONS)
        name = naming.image_name(self.cfg.talos_version, schematic)
        legacy = naming.legacy_image_name(self.cfg.talos_version)
        images = [
            img
            for img in (self.conn.image.find_image(n) for n in (name, legacy))
            if img is not None
        ]
        if not images:
            info(f"image {name} not found, nothing to remove")
            return
        found = ", ".join(img.name for img in images)
        log(f"remove image {found}")
        warn("other clusters on the same talos version may share this image")
        if not assume_yes and not dry_run():
            resp = input(f"type '{found}' to confirm: ").strip()
            if resp != found:
                raise SystemExit("aborted")
        for img in images:
            action(f"delete image {img.name}")
            if not dry_run():
                try:
                    self.conn.image.delete_image(img.id)
                except os_exceptions.SDKException as exc:
                    raise ReconcileError(
                        f"could not delete image {img.name}: {exc}\n"
                        "On Ceph-backed clouds (like Radiant) each boot volume is a "
                        "copy-on-write clone of the image, so the image cannot be deleted "
                        "while any cluster's nodes still exist. Note: you usually do NOT "
                        "need to delete the image -- `taloscluster image download` updates "
                        "its properties in place. To rebuild it, `destroy` the dependent "
                        "cluster(s) first, then `image remove`."
                    ) from exc

    def destroy_summary(self, inventory: InfrastructureInventory) -> str:
        raw = self._raw(inventory)
        return (
            f"{len(raw.all('servers'))} servers, {len(raw.all('ports'))} ports, "
            f"{len(raw.all('ips'))} floating ips, network + router + security group"
        )

    @_reconcile_errors
    def destroy_resources(self, inventory: InfrastructureInventory) -> None:
        raw = self._raw(inventory)
        for host in list(raw.all("servers")):
            compute.delete_node(self.conn, host, raw)
        for name, floating_ip in list(raw.all("ips").items()):
            action(f"delete floating ip {name}")
            if not dry_run():
                self.conn.network.delete_ip(floating_ip.id)
        for name, port in list(raw.all("ports").items()):
            action(f"delete port {name}")
            if not dry_run():
                try:
                    self.conn.network.delete_port(port.id)
                except os_exceptions.SDKException as exc:
                    warn(f"could not delete port {name}: {exc}")
            raw.drop("ports", name)
        managed_subnets = list(raw.all("subnets").values())
        for name, router in list(raw.all("routers").items()):
            action(f"delete router {name}")
            if not dry_run():
                for subnet in managed_subnets:
                    try:
                        self.conn.network.remove_interface_from_router(router, subnet=subnet.id)
                    except os_exceptions.SDKException as exc:
                        warn(f"could not detach subnet from router {name}: {exc}")
                self.conn.network.delete_router(router.id)
        for name, subnet in list(raw.all("subnets").items()):
            action(f"delete subnet {name}")
            if not dry_run():
                self.conn.network.delete_subnet(subnet.id)
        for name, net in list(raw.all("networks").items()):
            action(f"delete network {name}")
            if not dry_run():
                self.conn.network.delete_network(net.id)
        for name, group in list(raw.all("security_groups").items()):
            action(f"delete security group {name}")
            if not dry_run():
                self.conn.network.delete_security_group(group.id)
