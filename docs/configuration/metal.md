# Metal

Back to the [configuration index](../configuration.md).

A `metal:` section brings bare-metal machines into the cluster. It may sit beside the one [OpenStack](openstack.md) or [Proxmox](proxmox.md) section — VMs and bare metal sharing one cluster — or stand alone when every machine is bare metal; at most one VM provider may be set, with or without `metal`.

## `metal`

Optional · mapping of group names to groups

Each key names a group of bare-metal machines and maps to that group's settings, which must be a mapping. The section may also live in a file pulled in with [`include`](general.md#include), so a long machine list can stay out of `cluster.yaml`.

```yaml
metal:
  phoenix:
    role: worker
    redfish: true
    disk: /dev/sda
    network:                            # optional; defaults to network.cluster
      cidr: 172.29.21.0/24
      gateway: 172.29.21.1
      mtu: 9000
    interfaces:
      enp1s0f0: { role: pxe }
      enp2s0f0: { role: [cluster, external], dns: [192.0.2.53] }
    bmc:
      username: root
      password: CHANGE-ME
    servers:
      rp001:
        bmc: { ip: 172.28.50.5 }
        interfaces:
          enp2s0f0: { ip: 172.29.21.5/24 }
```

A group states the defaults every machine in `servers` starts from, and each server overrides them for its own machine: plain settings (`role`, `redfish`, `disk`, `network`) are replaced when the server sets one, while `bmc` merges key by key and `interfaces` merge per interface, so the group can carry the credentials and the cabling plan and each server adds only its own addresses. The commands that join the machines come with the metal provider.

### `metal.<group>.role`

Required · `controlplane` or `worker`

The Kubernetes role of every machine in the group; a server may override it.

### `metal.<group>.redfish`

Optional · boolean · default `false`

Whether taloscluster may talk to the machines' BMCs. Redfish is how the machines are powered and booted from their install media; `false` never touches the BMC, so the operator boots the machines into maintenance mode themselves.

### `metal.<group>.disk`

Required · non-empty string

The device the machines install Talos onto, such as `/dev/sda`; a server may override it for hardware that names its disks differently.

### `metal.<group>.network`

Optional · mapping · default [`network.cluster`](network.md#networkcluster)

The layer-2 network this group's machines sit on, with the same keys as [`network.cluster`](network.md#networkcluster). A group on the same L2 as the VM provider's machines omits it.

### `metal.<group>.interfaces`

Optional · mapping of interface name to interface

Each key is a machine's interface name as the OS will see it, and each interface says what the link is for and, where it is statically addressed, with which address. A server may override an interface's settings by name and add interfaces of its own.

### `metal.<group>.interfaces.<name>.role`

Required · `cluster`, `external`, `pxe`, or a list of those

What the link is for: `pxe` is the boot/maintenance link, `cluster` carries the [`network.cluster`](network.md#networkcluster) node network and `external` the [`network.external`](network.md#networkexternal) one. One interface may carry several roles as a list — a NIC that sits on both the cluster and the external network is `[cluster, external]`.

### `metal.<group>.interfaces.<name>.ip`

Optional · IPv4 address with an optional `/prefix` · default none

The static address of the link.

### `metal.<group>.interfaces.<name>.dns`

Optional · list of IP addresses · default none

The resolvers written for this link.

### `metal.<group>.bmc`

Optional · mapping

The Redfish settings every machine in the group starts from: `ip` (the BMC's IPv4 address), `username` and `password`. The credentials are ordinary cluster settings: like every other key they may live in `secrets.yaml` or any included file instead of `cluster.yaml`.

### `metal.<group>.servers`

Optional · mapping of machine name to overrides

Each key names one machine and maps to the settings that machine overrides, which are the group's own keys except `servers`. The name must be a valid hostname, the same name may not appear in two groups, and a machine with no overrides inherits the group as written.

On a cluster with no VM provider, the [pools](pools.md) carry only `count` and `disk`: neither VM provider's sizing keys apply, and the [`controlplane` pool](pools.md#controlplane) is still required.
