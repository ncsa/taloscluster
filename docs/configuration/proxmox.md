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
      vlan: 100
      kubeapi_vip: 10.0.0.10
    external:
      bridge: vmbr0
      vlan: 200
      cidr: 203.0.113.0/24
      gateway: 203.0.113.1
      anchor_cidr: 169.254.32.0/20
      ingress_pool: 203.0.113.20-203.0.113.29
```

### `proxmox.url`

Required · URL

The Proxmox server origin. The `/api2/json` path is added internally; URLs that already include it still work.

### `proxmox.storage`

Required · storage id

Storage used when creating VM boot disks. Changing it does not migrate existing disks.

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

The private network every VM's first NIC attaches to. Set exactly one of `bridge`, `vnet` or `sdn`. Moving a running cluster to another bridge, VLAN or VNet, or switching between `bridge` and `sdn`, is refused; recreate the cluster instead.

### `bridge`

One of · bridge name

An existing Linux bridge on the hosts, such as `vmbr0`.

### `vnet`

One of · VNet id

An existing Proxmox SDN VNet.

### `vlan`

Optional · 1 to 4094

VLAN tag on the NIC. Not allowed together with `sdn`.

### `kubeapi_vip`

Required here or under `external` · IPv4 inside `network.cidr`

The address control planes share as a Layer 2 VIP for the Kubernetes API. Set it in exactly one of `cluster` or `external`. Changing it later moves the API endpoint of the running cluster without a reboot. On a managed SDN it may not collide with the anycast gateway, a node's static address, or any address the static layout reserves.

## `proxmox.network.cluster.sdn`

Optional · mapping, may be empty

Replaces `bridge` or `vnet` with a managed EVPN network that taloscluster creates: an EVPN zone, a VNet and an SNAT subnet from `network.cidr`. `sdn: {}` accepts every default. Nodes get static addresses from `network.cidr`: the anycast gateway at the first host, controlplane-01 at host offset 11, and the first worker at offset 61. Control planes reserve offsets 10–59; each worker pool reserves a 50-address block beginning at offset 60 plus 50 times its zero-based position in file order. Each pool supports at most 49 nodes, and the subnet must be large enough for their addresses. `network.dns` must be set because the overlay has no DHCP, and `network.cidr` cannot change afterwards. The Proxmox hosts need FRR, IP forwarding and firewall rules for BGP and VXLAN; see [Proxmox setup](../providers/proxmox.md#managed-evpn-sdn).

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

Optional · list of node names · default `sdn.nodes`, else `proxmox.nodes`, else every online node

Hosts that route traffic out of the overlay. List only hosts with a routed external address; an exit node without one blackholes its VMs' egress.

### `sdn.primary_exit_node`

Optional · node name · default the first exit node

Preferred exit node. Must be one of the exit nodes.

### `sdn.mtu`

Optional · integer greater than zero · default unset

VNet MTU, typically the underlay MTU minus 50 bytes of VXLAN overhead. Changing it later needs a full stop and start of each VM. Removing it does not unset it on the zone.

### `sdn.nodes`

Optional · list of node names · default all nodes

Restrict the zone to these hosts. Also set `proxmox.nodes` to a matching compute-node set when other eligible hosts exist; converge rejects compute nodes outside the zone rather than silently filtering them out. Removing it does not unset it on the zone.

## `proxmox.network.external`

Optional · mapping

Adds a second NIC on a directly routed external subnet. It can carry the API VIP and MetalLB ingress addresses without NAT; the API VIP may instead remain on the private cluster link. Adding or removing this section on a running cluster is refused. Control planes get an external routing table when the API VIP is external. With `ingress_pool`, every node gets the routing table and a connection-marking static pod for ingress replies.

### `bridge`

Required · bridge name

Bridge carrying the external subnet.

### `vlan`

Optional · 1 to 4094

VLAN tag on the external NIC.

### `cidr`

Required · IPv4 network

The externally routed subnet. Must not overlap `network.cidr`.

### `gateway`

Required · IPv4 inside `cidr`

The subnet's gateway.

### `anchor_cidr`

Required · IPv4 network inside `169.254.0.0/16`

Range each machine draws a deterministic link-local `/32` anchor address from, because Talos will not use an interface without an address. Use `/20` or larger; an address collision aborts the run.

### `kubeapi_vip`

Optional · IPv4 inside `cidr`, outside `ingress_pool`

The API VIP on the external subnet. Set it here or under `cluster`, not both.

### `ingress_pool`

Optional · `start-end` IPv4 range inside `cidr`

Range reserved in your address plan for MetalLB ingress. Install and configure MetalLB separately to announce it; core taloscluster does not create a MetalLB address pool. When set, every machine runs a small static pod that marks connections entering the external NIC so replies to reverse-NATed traffic return through the external gateway. Edits apply through the machine config on the next converge.

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

The token secret.
