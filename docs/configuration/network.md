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
    gateway: 10.0.0.1       # route-MTU clamp on DHCP-backed Proxmox L2s
    vlan: 100               # Proxmox only
    mtu: 9000               # applied to the node links
    kubeapi_vip: 10.0.0.200 # Proxmox only
```

### `network.cluster.cidr`

Required · IPv4 network

The network the nodes' private addresses come from, written as a network address (`10.0.0.0/24`, not a host inside it). What it means depends on the provider:

- **OpenStack**: becomes the tenant subnet converge creates.
- **Proxmox with `bridge` or `vnet`**: must match the DHCP-served subnet on that link.
- **Proxmox managed SDN**: the overlay subnet. Nodes get static addresses from it, so it cannot change once the cluster runs. See [Proxmox](proxmox.md#proxmoxnetworkclustersdn).

It must not overlap the Kubernetes pod or service networks, which Talos defaults to `10.244.0.0/16` and `10.96.0.0/12`. A node whose own addresses fall inside either cannot tell its traffic from cluster traffic, and Talos raises its [`address-overlap`](https://talos.dev/latest/advanced/troubleshooting-control-plane/) diagnostic on it while kubelet, DNS and service routing misbehave. `check` and `converge` warn when any stated host network — this one, [`network.external.cidr`](#networkexternalcidr), [`network.external.anchor_cidr`](#networkexternalanchor_cidr) or a [metal group's](metal.md#metalgroupnetwork) — collides, naming the key. The warning reads Talos's defaults, so a cluster that moves the subnets with a `cluster.network.podSubnets`/`serviceSubnets` patch under [`talos.config_patches`](general.md#talosconfig_patches) is outside what it can see; so is an address a DHCP server hands a link `cluster.yaml` does not describe, such as a metal machine's PXE NIC, which only the node's own diagnostic catches.

### `network.cluster.gateway`

Optional · IPv4 address inside `cidr` · default none

The default gateway on this network. A managed SDN uses the first host of `cidr` as its anycast gateway and OpenStack sets the subnet's gateway itself, so on the VM providers the value is read only on a DHCP-backed Proxmox bridge or VNet: with a jumbo [`network.cluster.mtu`](#networkclustermtu) it is the gateway of the default route the machine configuration restates with an MTU of 1500, and it must be the gateway the DHCP server actually hands out. [Metal](metal.md) machines read it too, and need it: their links are statically addressed with DHCP off, so without a gateway the machine has no default route at all and reaches nothing beyond its own subnet. A group whose machines sit on a different L2 must name a gateway of its own under [`metal.<group>.network`](metal.md#metalgroupnetwork), and one that rides this network — the default — requires the gateway to be set here (on a managed SDN or an OpenStack tenant network, the first host of `cidr`).

### `network.cluster.vlan`

Optional · 1 to 4094 · default untagged

VLAN tag for the node NIC. Proxmox only: it becomes the VM NIC tag, and it is refused together with a managed SDN and with `openstack`, whose tenant network carries no tag.

### `network.cluster.mtu`

Optional · integer, at least 1280 · default `1500`

The MTU of this layer-2 network. Above 1500 it is written into the generated machine configuration: the node's link carries the MTU, and the default route on that link is clamped to 1500, so off-subnet TCP is MSS-clamped and UDP fragmented even when the gateway silently drops jumbo frames, while on-subnet traffic stays jumbo. On Proxmox the VM NIC is created with no MTU, so it inherits the bridge's and the bridge itself is the last link in that chain: `plan` warns when a compute node's bridge reads below this value (an interface without an explicit MTU reads as the 1500 default), and raising the bridge MTU on every node is the fix; a VM NIC carrying an explicit MTU has it stripped by converge so the NIC inherits — written to a stopped VM straight away and to a running one only at its next restart (`converge --reboot`), since Proxmox re-plugs a running VM's NIC and the re-plug takes the node off the pod network until flannel restarts. On OpenStack the tenant network is created stating this MTU so Neutron advertises it to the nodes, and the network phase warns when an existing network advertises less (one created by hand, or before converge stated it): a network left at the cloud default silently drops the large on-subnet frames the jumbo node links send, so its `mtu` must be raised on the cloud side. The route clamp applies wherever the tool knows the gateway — a managed SDN and an OpenStack subnet take the first host of `cidr` — and on a DHCP-backed Proxmox bridge or VNet only when [`network.cluster.gateway`](#networkclustergateway) is set; without it the DHCP-learned route keeps the link MTU. Every host on one layer-2 network must agree on the MTU, and changing it on a live cluster is a whole-cluster event — see [MTU](#mtu).

### `network.cluster.kubeapi_vip`

Required with `proxmox` · one of `network.cluster` / `network.external` · IPv4 inside `cidr`

The address control planes share as a Layer 2 VIP for the Kubernetes API. With `proxmox` it is required: exactly one of `network.cluster` and `network.external` must set it, and the load refuses the cluster without it. Changing it later moves the API endpoint of the running cluster by re-applying it through the machine config; it is not guaranteed to avoid a restart. On a managed Proxmox SDN it may not collide with the anycast gateway, a node's static address, or any address the static layout reserves. It is refused with `openstack`, where converge reserves the API address as a port with a floating IP in front of it. Like `ingress_pool`, it must sit outside any DHCP range on that network; see [Addresses outside the DHCP range](#addresses-outside-the-dhcp-range).

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

The MTU of the external network. Above 1500 it is stated explicitly on the external link's machine configuration, never inherited from another link (see [`network.cluster.mtu`](#networkclustermtu)), and the external default route — the one the ingress return path and an API VIP on the external network route their replies through — is clamped to 1500 like the private link's, so large replies survive a gateway that silently drops jumbo frames. On Proxmox the external NIC is created with no MTU, so it inherits the external bridge's, `plan` warns when that bridge reads below this value, and converge rewrites NICs of existing VMs that do not yet inherit.

### `network.external.kubeapi_vip`

Required with `proxmox` · one of `network.cluster` / `network.external` · IPv4 inside `cidr`, outside `ingress_pool`

The API VIP on the external subnet. Set it here or under `network.cluster`, not both; see [`network.cluster.kubeapi_vip`](#networkclusterkubeapi_vip) for what moving it costs. Like `ingress_pool`, it must sit outside any DHCP range on that network; see [Addresses outside the DHCP range](#addresses-outside-the-dhcp-range).

### `network.external.anchor_cidr`

Required · IPv4 network inside `169.254.0.0/16`

The range each machine draws a deterministic link-local `/32` anchor address from, because Talos will not use an interface without an address. Use `/20` or larger; an address collision aborts the run.

### `network.external.ingress_pool`

Optional · `start-end` IPv4 range inside `cidr` · default none

The range reserved in your address plan for MetalLB ingress. Install and configure MetalLB separately to announce it; core taloscluster does not create an address pool. When it is set, every machine runs a small static pod that marks connections entering the external NIC so replies to reverse-NATed traffic return through the external gateway; edits apply through the machine config on the next converge. See [Load balancers and ingress](../load-balancer.md#proxmox).

## Addresses outside the DHCP range

`kubeapi_vip` and `ingress_pool` name addresses taloscluster hands to the cluster itself, so they must lie outside any DHCP range serving that layer-2 network, and outside the addresses your own hosts use. taloscluster cannot see the DHCP server's pool and does not check this: a VIP inside the pool works until the day the server leases it to something else.

## MTU

There is no path MTU discovery inside a subnet: a packet larger than a peer's MTU is dropped at layer 2 and no ICMP comes back, so the sender never learns to send smaller. Every host on the network — the nodes, the hypervisors, the gateway, anything else on that wire — must therefore carry the same MTU as [`network.cluster.mtu`](#networkclustermtu) (or [`network.external.mtu`](#networkexternalmtu) on the external network), not just the nodes taloscluster writes it on. Changing an `mtu` on a live cluster is a whole-cluster event, not a rolling change: the machine configuration states it on every node, and on Proxmox the bridge MTU must be raised on every node with it.

The tool checks what it can see — `plan` warns when a compute node's cluster or external bridge reads below the configured MTU, and when the OpenStack tenant network advertises below the cluster MTU — but the gateway's MTU is outside its view, so where the default route is not clamped to 1500 (a DHCP-backed Proxmox network without [`network.cluster.gateway`](#networkclustergateway)) off-subnet traffic rides the DHCP-learned route at the link MTU and the gateway's jumbo support is on you. Verify what the path really carries from any Linux host on the network:

```bash
ping -M do -s 8972 10.0.0.1
```

`-M do` forbids fragmentation, and 8972 bytes of payload plus the 28 bytes of IP and ICMP headers is exactly the 9000-byte packet a jumbo MTU promises, so a reply proves the path to that address — another host on the L2 or the gateway — really carries it; silence or `message too long` means a hop is still at 1500.

## `network.dns`

Required · list of IP address strings, may be empty except on managed Proxmox SDN

On OpenStack, these are the subnet's DHCP nameservers; converge reconciles them on the existing subnet in place, so editing the list applies to the running cluster (a changed list is reported by `plan`). On Proxmox managed SDN, they are applied to each node through Talos `ResolverConfig`, including on later converges. Proxmox on an existing bridge or VNet uses DHCP-provided DNS; this list does not override it, so converge warns that `network.dns` has no effect there — the reason the Proxmox scaffold ships an empty list, which a switch to managed SDN (no DHCP) needs filled in.

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
