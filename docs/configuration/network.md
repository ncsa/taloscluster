# Network

Back to the [configuration index](../configuration.md).

```yaml
network:
  cidr: 10.0.0.0/24
  dns: [192.0.2.53, 198.51.100.53]
  ntp: [ntp.example.edu]
```

## `network.cidr`

Required · IPv4 network

The network the nodes' private addresses come from, written as a network address (`10.0.0.0/24`, not a host inside it). What it means depends on the provider:

- **OpenStack**: becomes the tenant subnet converge creates.
- **Proxmox with `bridge` or `vnet`**: must match the DHCP-served subnet on that link.
- **Proxmox managed SDN**: the overlay subnet. Nodes get static addresses from it, so it cannot change once the cluster runs. See [Proxmox](proxmox.md#proxmoxnetworkclustersdn).

## `network.cluster`

Optional · mapping · the node L2 described by `network.cidr`

The layer-2 network the nodes sit on, described in one place: `cidr` (the same network as `network.cidr`; setting both with different values is an error), optional `gateway`, optional `vlan` (1-4094), optional `mtu` (default 1500, at least 1280) and optional `kubeapi_vip`. `anchor_cidr` and `ingress_pool` belong to `network.external` and are refused here.

```yaml
network:
  cluster:
    cidr: 192.0.2.0/24
    gateway: 192.0.2.1
    mtu: 9000
    kubeapi_vip: 192.0.2.200
```

## `network.external`

Optional · mapping · no external network

The externally routed layer-2 network, for clusters that reach the outside world directly instead of through a provider-allocated network. It takes the same keys as `network.cluster` plus `anchor_cidr` (a range inside `169.254.0.0/16`) and `ingress_pool` (a `start-end` range inside its `cidr`). A cluster that has this network at all must describe it fully: `cidr`, `gateway` and `anchor_cidr` are required, and `cidr` must not overlap the cluster network. Exactly one of `network.cluster.kubeapi_vip` and `network.external.kubeapi_vip` may be set, and the VIP must not fall inside `ingress_pool`. Both `kubeapi_vip` and `ingress_pool` must lie outside any DHCP range on that network; taloscluster cannot check that for you.

```yaml
network:
  external:
    cidr: 198.51.100.0/24
    gateway: 198.51.100.1
    vlan: 100
    kubeapi_vip: 198.51.100.10
    anchor_cidr: 169.254.32.0/20
    ingress_pool: 198.51.100.190-198.51.100.199
```

## `network.dns`

Required · list of IP address strings, may be empty except on managed Proxmox SDN

On OpenStack, these are the subnet's DHCP nameservers; converge reconciles them on the existing subnet in place, so editing the list applies to the running cluster (a changed list is reported by `plan`). On Proxmox managed SDN, they are applied to each node through Talos `ResolverConfig`, including on later converges. Proxmox on an existing bridge or VNet uses DHCP-provided DNS; this list does not override it, so converge warns that `network.dns` has no effect there.

## `network.ntp`

Required · list of hostnames or addresses

NTP servers configured on every node.
