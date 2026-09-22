# Proxmox provider

Back to the [configuration index](../configuration.md).

Pools on Proxmox size their VMs with `cores` and `memory` (see [Pools](pools.md)). Every VM gets a first NIC on the cluster network; an optional second NIC carries a directly routed external subnet.

## cluster.yaml

```yaml
proxmox:
  url: https://pve.example.edu:8006
  storage: local-lvm
  iso_storage: local
  cidata_storage: local
  placement_strategy: spread
  nodes: [pve1, pve2, pve3]
  tls_verify: true
  network:
    cluster:
      bridge: vmbr0
    external:
      bridge: vmbr1
```

The addresses on those networks live under [`network`](network.md):

```yaml
network:
  cluster:
    cidr: 10.0.0.0/24
    vlan: 10
    kubeapi_vip: 10.0.0.10
  external:
    vlan: 100
    cidr: 203.0.113.0/24
    gateway: 203.0.113.1
    anchor_cidr: 169.254.32.0/20
    ingress_pool: 203.0.113.20-203.0.113.29
```

### `proxmox.url`

Required · URL

The Proxmox server origin. The `/api2/json` path is added internally; URLs that already include it still work. The cluster must run Proxmox 9 or newer, which converge checks against the first node and refuses below: Proxmox 9 makes a VM NIC inherit its bridge's MTU from an unset MTU, while 8 and earlier default it to 1500 and need an `mtu=1` sentinel to inherit — the opposite convention, and one 9 reads as a literal MTU of 1, which costs the node its network. See the [Proxmox 9.0 known issues](https://pve.proxmox.com/wiki/Roadmap#9.0-known-issues).

### `proxmox.storage`

Required · storage id

Storage used when creating VM boot disks. Changing it does not migrate existing disks, so converge refuses the change with recreation guidance instead of silently leaving existing VMs on the old storage.

### `proxmox.iso_storage`

Required · storage id

Storage the Talos boot ISO is downloaded to. Must be visible on every compute node the cluster uses.

### `proxmox.cidata_storage`

Optional · storage id · default `local`

Node-local storage for the per-VM cloud-init volume that briefly carries the machine configuration and secrets. A shared storage is refused.

### `proxmox.placement_strategy`

Optional · `spread` · default `spread`

How new VMs are placed. `spread` is the only accepted value. Control planes prefer hosts that do not already hold a control plane; workers choose the eligible host with the most available memory. Memory is reserved for earlier choices in the same run. Existing VMs are not rebalanced.

### `proxmox.nodes`

Optional · list of node names · default online nodes with access to all required storages

Proxmox nodes VMs may be placed on. A pool's `node` must be a member. On a managed SDN every entry must also be inside `sdn.nodes` when that is set.

### `proxmox.tls_verify`

Optional · `true`, `false`, or a path · default `true`

Verify the API certificate against the system trust store, skip verification, or verify against the given CA bundle file.

## `proxmox.network.cluster`

Required · mapping

The private network every VM's first NIC attaches to. Set exactly one of `bridge`, `vnet` or `sdn`. The addresses on that network — cidr, gateway, VLAN tag, MTU and the API VIP — are described in [`network.cluster`](network.md#networkcluster). Moving a running cluster to another bridge, VLAN or VNet, or switching between `bridge` and `sdn`, is refused; recreate the cluster instead. Only the keys documented below are accepted inside `proxmox.network`, `cluster`, `cluster.sdn` and `external`; a miscapped key (such as `vlna` or `bridg`) is refused at load instead of being silently ignored, and a key that has moved into `network.cluster` / `network.external` is refused with the name of its new location (see [Moving from the old keys](network.md#moving-from-the-old-keys)).

### `bridge`

One of · bridge name

An existing Linux bridge on the hosts, such as `vmbr0`.

### `vnet`

One of · VNet id

An existing Proxmox SDN VNet.

## `proxmox.network.cluster.sdn`

Optional · mapping, may be empty

Replaces `bridge` or `vnet` with a managed EVPN network that taloscluster creates: an EVPN zone, a VNet and an SNAT subnet from `network.cluster.cidr`. `sdn: {}` accepts every default. Nodes get static addresses from `network.cluster.cidr`: the anycast gateway at the first host, controlplane-01 at host offset 11, and the first worker at offset 61. Control planes reserve offsets 10–59; each worker pool reserves a 50-address block beginning at offset 60 plus 50 times its zero-based position in file order. Each pool supports at most 49 nodes, and the subnet must be large enough for their addresses. `network.dns` must be set because the overlay has no DHCP, and `network.cluster.cidr` cannot change afterwards. The bridge is verified on every converge and, because the apply task can return before each node's network reload finishes, converge waits up to a minute for it to appear before reporting a node that still lacks it. The Proxmox hosts need FRR, IP forwarding and firewall rules for BGP and VXLAN; see [Proxmox setup](../providers/proxmox.md#managed-evpn-sdn).

```yaml
proxmox:
  network:
    cluster:
      sdn:
        name: mycl
        asn: 65000
        exit_nodes: [pve1, pve3]
        primary_exit_node: pve1
        mtu: 8950

network:
  cluster:
    cidr: 10.0.0.0/24
    kubeapi_vip: 10.0.0.2
```

### `sdn.name`

Optional · 2 to 8 characters, letter first, no hyphens · default the cluster name

Zone and VNet id. A longer or hyphenated cluster name must set this.

### `sdn.zone`

Optional · `evpn` · default `evpn`

Zone type. Only EVPN is supported.

### `sdn.controller`

Optional · controller id · default `evpnctl`

EVPN controller. Created with peers from the Proxmox cluster when missing and never updated afterwards, so a host added later must be added to its peers by hand.

### `sdn.asn`

Optional · 0 to 4294967295 · default `65000`

BGP autonomous system number for the controller.

### `sdn.vrf_tag`

Optional · 1 to 16777215 · default derived from the cluster name

VXLAN id of the zone's VRF.

### `sdn.tag`

Optional · 1 to 16777215, not equal to `vrf_tag` · default `vrf_tag + 1`

VXLAN id of the VNet.

### `sdn.exit_nodes`

Optional · list of node names · default `sdn.nodes`, else `proxmox.nodes` (every cluster node, offline included)

Hosts that route traffic out of the overlay. List only hosts with a routed external address; an exit node without one blackholes its VMs' egress.

### `sdn.primary_exit_node`

Optional · node name · default the first exit node

Preferred exit node. Must be one of the exit nodes.

### `sdn.mtu`

Optional · integer, [`network.cluster.mtu`](network.md#networkclustermtu) or higher · default unset

VNet MTU, typically the underlay MTU minus 50 bytes of VXLAN overhead. It cannot sit below [`network.cluster.mtu`](network.md#networkclustermtu): guest NICs inherit the VNet MTU, so frames above the zone MTU are dropped. Changing it later needs a full stop and start of each VM. Removing it does not unset it on the zone.

### `sdn.nodes`

Optional · list of node names · default all nodes

Restrict the zone to these hosts. Also set `proxmox.nodes` to a matching compute-node set when other eligible hosts exist; converge rejects compute nodes outside the zone rather than silently filtering them out. Removing it does not unset it on the zone.

## `proxmox.network.external`

Optional · mapping

Adds a second NIC on a directly routed external subnet, described in [`network.external`](network.md#networkexternal); neither half works without the other, so a bridge here without that block, or that block without a bridge here, is refused. It can carry the API VIP and MetalLB ingress addresses without NAT; the API VIP may instead remain on the private cluster link. Adding or removing this section on a running cluster is refused. Control planes get an external routing table when the API VIP is external. With `ingress_pool`, every node gets the routing table and a connection-marking static pod for ingress replies.

### `bridge`

Required · bridge name

Bridge carrying the external subnet.

## secrets.yaml

```yaml
proxmox:
  token_id: "taloscluster@pve!provider"
  token_secret: "CHANGE-ME"
```

### `proxmox.token_id`

Required · `user@realm!tokenname`

Proxmox API token id. Provider operations that load Proxmox inventory run a read-only permission preflight before mutation and reports missing privileges with their ACL paths. See [Proxmox API token permissions](../providers/proxmox.md#proxmox-api-token-permissions) for the required privileges.

### `proxmox.token_secret`

Required · string

The token secret. Must be a real, non-empty string that is not the scaffolded `CHANGE-ME` placeholder; a null, non-string, empty, or placeholder value is refused when a command needs the credential instead of failing later as an opaque 401.
