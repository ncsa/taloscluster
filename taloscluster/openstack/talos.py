"""OpenStack's contribution to a node's Talos machine configuration.

OpenStack needs the virtio boot disk and the network documents that keep DHCP
on ``eth0`` and put the Layer 2 API VIP on it. Control planes also carry a
bootstrap NetworkPolicy keeping pods off the Nova metadata service, which
serves the machine config delivered as user_data for the instance's lifetime.
"""

from __future__ import annotations

import ipaddress

import yaml

from ..config import Config, Machine
from ..infrastructure import Endpoint, TalosContribution, TalosPatch, dhcp_link_documents

# OpenStack servers boot from a virtio-blk disk.
INSTALL_DISK = "/dev/vda"
INSTALLER_PLATFORM = "openstack"

# The machine config travels as Nova user_data, and Nova keeps serving it at
# the metadata address for the instance's whole lifetime -- pod egress to it is
# masqueraded behind the node, so an unprivileged pod could read a control
# plane's config (the CA keys, the join tokens, the tailscale key) and take
# over the cluster. The Talos firewall is ingress-only, so the block ships as a
# NetworkPolicy in the bootstrap manifests instead: pods in the default
# namespace may egress anywhere except the metadata address. It enforces
# nothing under a CNI that ignores NetworkPolicy (Talos's default, Flannel),
# and namespaces created after bootstrap are not covered -- the residual
# exposure is stated in docs/providers/openstack.md.
CLOUD_METADATA_CIDR = "169.254.169.254/32"
METADATA_POLICY_NAME = "block-cloud-metadata"


def _subnet_gateway(cidr: str) -> str:
    """The gateway the DHCP lease hands out on the tool-created subnet.

    Neutron gives a subnet with no explicit gateway the first host of its CIDR,
    and the subnet's DHCP router option is that gateway, so restating the same
    default route in the machine configuration replaces the lease's route.
    """
    return str(ipaddress.IPv4Network(cidr).network_address + 1)


def metadata_policy_patch() -> TalosPatch:
    """The bootstrap NetworkPolicy denying pods the metadata service.

    The control plane's cluster config embeds the manifest under
    ``cluster.inlineManifests``, so Talos applies it with the rest of the
    bootstrap manifests. Egress is allowed everywhere except the metadata
    address, so the policy changes nothing else about pod networking.
    """
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": METADATA_POLICY_NAME, "namespace": "default"},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {"ipBlock": {"cidr": "0.0.0.0/0", "except": [CLOUD_METADATA_CIDR]}}
                    ]
                }
            ],
        },
    }
    return TalosPatch(
        "metadata-policy",
        {
            "cluster": {
                # Talos's ClusterConfig.inlineManifests is []v1alpha1.
                # ClusterInlineManifest ({name, contents}); a map-shaped patch
                # fails every `talosctl gen config` (v1.13+).
                "inlineManifests": [
                    {
                        "name": METADATA_POLICY_NAME,
                        "contents": yaml.safe_dump(policy, sort_keys=False),
                    }
                ]
            }
        },
    )


def contribution(m: Machine, cfg: Config, endpoint: Endpoint) -> TalosContribution:
    vip = endpoint.vip if m.role == "controlplane" else None
    patches = [
        TalosPatch(
            "network",
            dhcp_link_documents(
                "eth0",
                vip,
                mtu=cfg.network.cluster.mtu,
                gateway=_subnet_gateway(cfg.network.cluster.cidr),
            ),
        ),
    ]
    # bootstrap manifests are read from the control plane's config, so only
    # control planes need to carry the metadata policy
    if m.role == "controlplane":
        patches.append(metadata_policy_patch())
    return TalosContribution(install_disk=INSTALL_DISK, patches=tuple(patches))
