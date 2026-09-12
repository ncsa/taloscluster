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

## `network.dns`

Required · list of IP address strings, may be empty except on managed Proxmox SDN

On OpenStack, these are the subnet's DHCP nameservers; converge reconciles them on the existing subnet in place, so editing the list applies to the running cluster (a changed list is reported by `plan`). On Proxmox managed SDN, they are applied to each node through Talos `ResolverConfig`, including on later converges. Proxmox on an existing bridge or VNet uses DHCP-provided DNS; this list does not override it, so converge warns that `network.dns` has no effect there.

## `network.ntp`

Required · list of hostnames or addresses

NTP servers configured on every node.
