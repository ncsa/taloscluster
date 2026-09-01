"""OpenStack implementation of the provider-neutral infrastructure boundary."""

from __future__ import annotations

import shlex

from openstack import exceptions as os_exceptions

from .. import naming
from ..config import Config, Machine, OpenStackConfig, OpenStackSecrets, Secrets
from ..errors import ConfigError
from ..infrastructure import (
    Endpoint,
    InfrastructureInventory,
    InfrastructureMachine,
    NetworkAttachment,
    NetworkResult,
)
from ..output import action, dry_run, info, log, warn
from . import compute, image, network, security
from .network import _fixed_ip
from .session import REGION, Inventory, connect, project_name

_STATUS_KINDS = (
    "networks", "subnets", "routers", "security_groups", "ports", "ips", "servers",
)


class OpenStackBackend:
    name = "openstack"

    def __init__(self, cfg: Config, secrets: Secrets):
        if not isinstance(cfg.provider, OpenStackConfig):
            raise ConfigError("OpenStack backend requires openstack configuration")
        if not isinstance(secrets.provider, OpenStackSecrets):
            raise ConfigError("OpenStack backend requires openstack credentials")
        self.cfg = cfg
        self.secrets = secrets
        self.conn = connect(cfg, secrets)

    def load_inventory(self) -> InfrastructureInventory:
        raw = Inventory(self.conn, self.cfg.name).load()
        machines: dict[str, InfrastructureMachine] = {}
        for name, server in raw.all("servers").items():
            port = raw.get("ports", name)
            attachment = NetworkAttachment(
                name=name,
                address=_fixed_ip(port),
                provider_id=str(getattr(port, "id", "") or ""),
            )
            machines[name] = InfrastructureMachine(
                name=name,
                provider_id=str(getattr(server, "id", "") or ""),
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
            raise RuntimeError("OpenStack inventory is unavailable")
        return raw

    def ensure_boot_artifact(self) -> str:
        return image.ensure_image(self.conn, self.cfg)

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

        return NetworkResult(
            kubernetes=endpoint(naming.kubeapi_name(self.cfg.name)),
            ingress=endpoint(naming.ingress_name(self.cfg.name)),
        )

    def reconcile_machines(
        self,
        machines: dict[str, Machine],
        inventory: InfrastructureInventory,
        boot_artifact: str,
        configs: dict[str, str],
    ) -> None:
        compute.reconcile(
            self.conn,
            self.cfg,
            machines,
            self._raw(inventory),
            boot_artifact,
            configs,
        )

    def delete_machine(self, name: str, inventory: InfrastructureInventory) -> None:
        compute.delete_node(self.conn, name, self._raw(inventory))

    def finalize_machines(self, inventory: InfrastructureInventory) -> None:
        return None

    def default_node_tags(self) -> dict[str, str]:
        project = project_name(self.conn)
        return {"ncsa/project": project} if project else {}

    def provider_status(self) -> dict[str, str]:
        return {
            "url": self.cfg.provider.url,
            "region": REGION,
            "project": project_name(self.conn),
        }

    def print_environment(self) -> None:
        provider = self.cfg.provider
        print(f"export OS_AUTH_URL={shlex.quote(provider.url)}")
        print("export OS_AUTH_TYPE=v3applicationcredential")
        print(f"export OS_REGION_NAME={shlex.quote(REGION)}")
        print(
            "export OS_APPLICATION_CREDENTIAL_ID="
            f"{shlex.quote(self.secrets.openstack_credential_id)}"
        )
        print(
            "export OS_APPLICATION_CREDENTIAL_SECRET="
            f"{shlex.quote(self.secrets.openstack_credential_secret)}"
        )

    def download_image(self) -> str:
        return image.ensure_image(self.conn, self.cfg)

    def remove_image(self, assume_yes: bool = False) -> None:
        name = naming.image_name(self.cfg.talos_version)
        img = self.conn.image.find_image(name)
        if img is None:
            info(f"image {name} not found, nothing to remove")
            return
        log(f"remove image {name}")
        warn("other clusters on the same talos version may share this image")
        if not assume_yes and not dry_run():
            resp = input(f"type '{name}' to confirm: ").strip()
            if resp != name:
                raise SystemExit("aborted")
        action(f"delete image {name}")
        if not dry_run():
            try:
                self.conn.image.delete_image(img.id)
            except os_exceptions.SDKException as exc:
                raise RuntimeError(
                    f"could not delete image {name}: {exc}\n"
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
