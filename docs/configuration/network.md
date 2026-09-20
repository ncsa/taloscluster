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

The layer-2 network the nodes sit on, described in one place for every provider. Which bridge, VNet or SDN the network is reached through is Proxmox plumbing and stays in [`proxmox.network.cluster`](proxmox.md#proxmoxnetworkcluster).

```yaml
network:
  cluster:
    cidr: 10.0.0.0/24
    gateway: 10.0.0.1       # accepted; not applied to nodes yet
    vlan: 100               # Proxmox only
    mtu: 9000               # validated; not applied to nodes yet
    kubeapi_vip: 10.0.0.200 # Proxmox only
```

### `network.cluster.cidr`

Required · IPv4 network

The network the nodes' private addresses come from, written as a network address (`10.0.0.0/24`, not a host inside it). What it means depends on the provider:

- **OpenStack**: becomes the tenant subnet converge creates.
- **Proxmox with `bridge` or `vnet`**: must match the DHCP-served subnet on that link.
- **Proxmox managed SDN**: the overlay subnet. Nodes get static addresses from it, so it cannot change once the cluster runs. See [Proxmox](proxmox.md#proxmoxnetworkclustersdn).

### `network.cluster.gateway`

Optional · IPv4 address inside `cidr` · default none

The default gateway on this network. Nothing reads it today: DHCP supplies the gateway on a Proxmox bridge or VNet, a managed SDN uses the first host of `cidr` as its anycast gateway, and OpenStack sets the subnet's gateway itself. It is accepted and validated for the statically addressed machines the bare-metal support will add.

### `network.cluster.vlan`

Optional · 1 to 4094 · default untagged

VLAN tag for the node NIC. Proxmox only: it becomes the VM NIC tag, and it is refused together with a managed SDN and with `openstack`, whose tenant network carries no tag.

### `network.cluster.mtu`

Optional · integer, at least 1280 · default `1500`

The MTU of this layer-2 network. The value is validated today but not yet written into the generated machine configuration: it is reserved for the jumbo-frame support, which will set the link MTU and clamp the route MTU. Every node on one layer-2 network must agree on the MTU.

### `network.cluster.kubeapi_vip`

One of `network.cluster` / `network.external` on Proxmox · IPv4 inside `cidr` · default none

The address control planes share as a Layer 2 VIP for the Kubernetes API. Set it in exactly one of `network.cluster` and `network.external`. Changing it later moves the API endpoint of the running cluster by re-applying it through the machine config; it is not guaranteed to avoid a restart. On a managed Proxmox SDN it may not collide with the anycast gateway, a node's static address, or any address the static layout reserves. It is refused with `openstack`, where converge reserves the API address as a port with a floating IP in front of it. Like `ingress_pool`, it must sit outside any DHCP range on that network; see [Addresses outside the DHCP range](#addresses-outside-the-dhcp-range).

## `network.external`

Optional · mapping · default no external network

The externally routed layer-2 network, for clusters that reach the outside world directly instead of through a provider-allocated network. Proxmox only: the bridge it is reached through stays in [`proxmox.network.external`](proxmox.md#proxmoxnetworkexternal), and the block is refused with `openstack`, which allocates the external network itself from [`openstack.external_net`](openstack.md#openstackexternal_net) — a router plus floating IPs for the API and ingress ports — at converge.

A cluster that has this network at all must describe it fully: `cidr`, `gateway` and `anchor_cidr` are required, and `cidr` must not overlap `network.cluster.cidr`.

```yaml
network:
  external:
    cidr: 203.0.113.0/24
    gateway: 203.0.113.1
    vlan: 100
    kubeapi_vip: 203.0.113.10
    anchor_cidr: 169.254.32.0/20
    ingress_pool: 203.0.113.190-203.0.113.199
```

### `network.external.cidr`

Required · IPv4 network

The externally routed subnet. It must not overlap `network.cluster.cidr`.

### `network.external.gateway`

Required · IPv4 address inside `cidr`

The subnet's gateway. Replies to externally initiated traffic are routed back through it.

### `network.external.vlan`

Optional · 1 to 4094 · default untagged

VLAN tag for the external NIC.

### `network.external.mtu`

Optional · integer, at least 1280 · default `1500`

The MTU of the external network, validated but not yet written into the machine configuration (see [`network.cluster.mtu`](#networkclustermtu)).

### `network.external.kubeapi_vip`

One of `network.cluster` / `network.external` on Proxmox · IPv4 inside `cidr`, outside `ingress_pool` · default none

The API VIP on the external subnet. Set it here or under `network.cluster`, not both; see [`network.cluster.kubeapi_vip`](#networkclusterkubeapi_vip) for what moving it costs. Like `ingress_pool`, it must sit outside any DHCP range on that network; see [Addresses outside the DHCP range](#addresses-outside-the-dhcp-range).

### `network.external.anchor_cidr`

Required · IPv4 network inside `169.254.0.0/16`

The range each machine draws a deterministic link-local `/32` anchor address from, because Talos will not use an interface without an address. Use `/20` or larger; an address collision aborts the run.

### `network.external.ingress_pool`

Optional · `start-end` IPv4 range inside `cidr` · default none

The range reserved in your address plan for MetalLB ingress. Install and configure MetalLB separately to announce it; core taloscluster does not create an address pool. When it is set, every machine runs a small static pod that marks connections entering the external NIC so replies to reverse-NATed traffic return through the external gateway; edits apply through the machine config on the next converge. See [Load balancers and ingress](../load-balancer.md#proxmox).

## Addresses outside the DHCP range

`kubeapi_vip` and `ingress_pool` name addresses taloscluster hands to the cluster itself, so they must lie outside any DHCP range serving that layer-2 network, and outside the addresses your own hosts use. taloscluster cannot see the DHCP server's pool and does not check this: a VIP inside the pool works until the day the server leases it to something else.

## `network.dns`

Required · list of IP address strings, may be empty except on managed Proxmox SDN

On OpenStack, these are the subnet's DHCP nameservers; converge reconciles them on the existing subnet in place, so editing the list applies to the running cluster (a changed list is reported by `plan`). On Proxmox managed SDN, they are applied to each node through Talos `ResolverConfig`, including on later converges. Proxmox on an existing bridge or VNet uses DHCP-provided DNS; this list does not override it, so converge warns that `network.dns` has no effect there.

## `network.ntp`

Required · list of hostnames or addresses

NTP servers configured on every node.

## Moving from the old keys

The L2 settings used to live partly under `network.cidr` and partly under `proxmox.network`. They are now all in `network.cluster` / `network.external`, and the old locations are refused at load with the name of their new home, so an old `cluster.yaml` fails fast instead of converging against half a configuration:

| Old key | New key |
| --- | --- |
| `network.cidr` | `network.cluster.cidr` |
| `proxmox.network.cluster.vlan` | `network.cluster.vlan` |
| `proxmox.network.cluster.kubeapi_vip` | `network.cluster.kubeapi_vip` |
| `proxmox.network.external.cidr` | `network.external.cidr` |
| `proxmox.network.external.gateway` | `network.external.gateway` |
| `proxmox.network.external.anchor_cidr` | `network.external.anchor_cidr` |
| `proxmox.network.external.kubeapi_vip` | `network.external.kubeapi_vip` |
| `proxmox.network.external.vlan` | `network.external.vlan` |
| `proxmox.network.external.ingress_pool` | `network.external.ingress_pool` |

`proxmox.network.cluster` keeps only `bridge`, `vnet` or `sdn`, and `proxmox.network.external` only `bridge`: the Proxmox section says which link the network is reached through, the `network` section says what is on it. Moving the keys changes no addresses, so a cluster converged from the old shape stays as it is.
