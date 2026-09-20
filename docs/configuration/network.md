# Network

Back to the [configuration index](../configuration.md).

```yaml
network:
  cluster:
    cidr: 10.0.0.0/24
  dns: [192.0.2.53, 198.51.100.53]
  ntp: [ntp.example.edu]
```

## `network.cluster`

Required · mapping

The layer-2 network the nodes sit on: `cidr` (required), optional `gateway`, optional `vlan` (1-4094), optional `mtu` (default 1500, at least 1280) and, on Proxmox, `kubeapi_vip` (IPv4 inside `cidr`). `anchor_cidr` and `ingress_pool` describe the external network and are refused here. OpenStack has no `kubeapi_vip` key: it allocates the API address itself through a reserved port and a floating IP.

`cidr` is the network the nodes' private addresses come from, written as a network address (`10.0.0.0/24`, not a host inside it). What it means depends on the provider:

- **OpenStack**: becomes the tenant subnet converge creates.
- **Proxmox with `bridge` or `vnet`**: must match the DHCP-served subnet on that link.
- **Proxmox managed SDN**: the overlay subnet. Nodes get static addresses from it, so it cannot change once the cluster runs. See [Proxmox](proxmox.md#proxmoxnetworkclustersdn).

On Proxmox, `vlan` is the VM NIC tag and is not allowed together with a managed SDN; which bridge or VNet the network is reached through stays in [`proxmox.network.cluster`](proxmox.md#proxmoxnetworkcluster).

On Proxmox, `kubeapi_vip` is the address control planes share as a Layer 2 VIP for the Kubernetes API, and must be set in exactly one of `network.cluster` or `network.external`. Changing it later moves the API endpoint of the running cluster by re-applying it through the machine config; it is not guaranteed to avoid a restart. On a managed Proxmox SDN it may not collide with the anycast gateway, a node's static address, or any address the static layout reserves.

```yaml
network:
  cluster:
    cidr: 192.0.2.0/24
    gateway: 192.0.2.1
    mtu: 9000
    kubeapi_vip: 192.0.2.200 # Proxmox only
```

## `network.external`

Optional · mapping · no external network

The externally routed layer-2 network, for clusters that reach the outside world directly instead of through a provider-allocated network (Proxmox only; the bridge it is reached through stays in [`proxmox.network.external`](proxmox.md#proxmoxnetworkexternal)). It takes the same keys as `network.cluster` plus `anchor_cidr` (a range inside `169.254.0.0/16`) and `ingress_pool` (a `start-end` range inside its `cidr`). A cluster that has this network at all must describe it fully: `cidr`, `gateway` and `anchor_cidr` are required, and `cidr` must not overlap the cluster network. Exactly one of `network.cluster.kubeapi_vip` and `network.external.kubeapi_vip` may be set, and the VIP must not fall inside `ingress_pool`. Both `kubeapi_vip` and `ingress_pool` must lie outside any DHCP range on that network; taloscluster cannot check that for you.

`anchor_cidr` is the range each machine draws a deterministic link-local `/32` anchor address from, because Talos will not use an interface without an address; use `/20` or larger, as an address collision aborts the run. `ingress_pool` is the range reserved in your address plan for MetalLB ingress: install and configure MetalLB separately to announce it, as core taloscluster does not create an address pool. When it is set, every machine runs a small static pod that marks connections entering the external NIC so replies to reverse-NATed traffic return through the external gateway; edits apply through the machine config on the next converge.

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
