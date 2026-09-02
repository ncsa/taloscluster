"""OpenStack's contribution to a node's Talos machine configuration.

OpenStack needs nothing more than the virtio boot disk and the legacy
``machine.network.interfaces`` block that puts the Layer 2 API VIP on ``eth0``.
"""

from __future__ import annotations

from ..config import Config, Machine
from ..infrastructure import Endpoint, TalosContribution, TalosPatch

# OpenStack servers boot from a virtio-blk disk.
INSTALL_DISK = "/dev/vda"
INSTALLER_PLATFORM = "openstack"


def contribution(m: Machine, cfg: Config, endpoint: Endpoint) -> TalosContribution:
    interfaces = (
        [{"interface": "eth0", "dhcp": True, "vip": {"ip": endpoint.vip}}]
        if m.role == "controlplane"
        else []
    )
    return TalosContribution(
        install_disk=INSTALL_DISK,
        patches=(
            TalosPatch("network", {"machine": {"network": {"interfaces": interfaces}}}),
        ),
    )
