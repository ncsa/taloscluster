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

Required · list of IPv4 addresses

Resolvers configured on every node. The list may be empty except on a managed Proxmox SDN, which has no DHCP to supply one.

## `network.ntp`

Required · list of hostnames or addresses

NTP servers configured on every node.
