"""OpenStack's contribution to a node's Talos machine configuration.

OpenStack needs nothing more than the virtio boot disk and the network
documents that keep DHCP on ``eth0`` and put the Layer 2 API VIP on it.
"""

from __future__ import annotations

import ipaddress

from ..config import Config, Machine
from ..infrastructure import Endpoint, TalosContribution, TalosPatch, dhcp_link_documents

# OpenStack servers boot from a virtio-blk disk.
INSTALL_DISK = "/dev/vda"
INSTALLER_PLATFORM = "openstack"


def _subnet_gateway(cidr: str) -> str:
    """The gateway the DHCP lease hands out on the tool-created subnet.

    Neutron gives a subnet with no explicit gateway the first host of its CIDR,
    and the subnet's DHCP router option is that gateway, so restating the same
    default route in the machine configuration replaces the lease's route.
    """
    return str(ipaddress.IPv4Network(cidr).network_address + 1)


def contribution(m: Machine, cfg: Config, endpoint: Endpoint) -> TalosContribution:
    vip = endpoint.vip if m.role == "controlplane" else None
    return TalosContribution(
        install_disk=INSTALL_DISK,
        patches=(
            TalosPatch(
                "network",
                dhcp_link_documents(
                    "eth0",
                    vip,
                    mtu=cfg.network.cluster.mtu,
                    gateway=_subnet_gateway(cfg.network.cluster.cidr),
                ),
            ),
        ),
    )
