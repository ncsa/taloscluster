# Metal

Back to the [configuration index](../configuration.md).

A `metal:` section brings bare-metal machines into the cluster. It sits beside the one [OpenStack](openstack.md) or [Proxmox](proxmox.md) section — VMs and bare metal sharing one cluster — and is refused without it: one VM provider is always required, since the provider backend plans, converges and destroys the cluster the metal machines join. `taloscluster init --metal` scaffolds the example group below, beside a provider, with the group's BMC credentials as `CHANGE-ME` placeholders in `secrets.yaml`. See [Metal setup](../providers/metal.md) for the preparation the machines and their BMCs need.

## `metal`

Optional · mapping of group names to groups

Each key names a group of bare-metal machines and maps to that group's settings, which must be a mapping. The section may also live in a file pulled in with [`include`](general.md#include), so a long machine list can stay out of `cluster.yaml`.

```yaml
metal:
  rack1:
    role: worker
    redfish: true
    disk: /dev/sda
    network:                            # optional; defaults to network.cluster
      cidr: 192.168.8.0/24
      gateway: 192.168.8.1
      mtu: 9000
    interfaces:
      enp1s0f0: { role: pxe }
      enp2s0f0: { role: [cluster, external], dns: [192.0.2.53] }
    servers:
      srv01:
        bmc: { ip: 192.168.8.51 }
        interfaces:
          enp2s0f0: { ip: 192.168.8.11/24 }
```

The group's BMC credentials are ordinary secrets: they live in `secrets.yaml` — or any file [`include`](general.md#include) names, the way `init --metal` scaffolds them — and merge into every machine in the group:

```yaml
# secrets.yaml
metal:
  rack1:
    bmc:
      username: root
      password: CHANGE-ME
```

A group states the defaults every machine in `servers` starts from, and each server overrides them for its own machine: plain settings (`role`, `redfish`, `disk`, `network`) are replaced when the server sets one, while `bmc` merges key by key and `interfaces` merge per interface, so the group can carry the credentials and the cabling plan and each server adds only its own addresses. The [`metal` commands](../commands.md#metal) then join the machines: they boot them from the Talos install ISO (through the BMC's virtual media, or served over the LAN with `--serve`), push the generated machine configuration to each machine in maintenance mode, and verify it came back with it.

### `metal.<group>.role`

Required · `controlplane` or `worker`

The Kubernetes role of every machine in the group; a server may override it.

### `metal.<group>.redfish`

Optional · boolean · default `false`

Whether taloscluster may talk to the machines' BMCs. Redfish is how the machines are powered and booted from their install media; `false` never touches the BMC, so the operator boots the machines into maintenance mode themselves (PXE, USB) and [`join`](../commands.md#metal) becomes wait, apply and verify, while `inspect`, `boot` and `eject` skip the BMC with a notice. Enabling it requires a BMC address and real BMC credentials for every machine in the group; see [`metal.<group>.bmc`](#metalgroupbmc).

### `metal.<group>.disk`

Required · non-empty string

The device the machines install Talos onto, such as `/dev/sda`; a server may override it for hardware that names its disks differently.

### `metal.<group>.network`

Optional · mapping · default [`network.cluster`](network.md#networkcluster)

The layer-2 network this group's machines sit on, with the same keys as [`network.cluster`](network.md#networkcluster) except `kubeapi_vip`: the API VIP is a cluster-wide address — with Proxmox read from [`network.cluster`](network.md#networkclusterkubeapi_vip) or [`network.external`](network.md#networkexternalkubeapi_vip), with OpenStack the address converge reserves for the API on the tenant network — and a metal network naming its own is refused rather than silently ignored. A group on the same L2 as the VM provider's machines omits it, and a server may override it with an L2 of its own. A group — or a single server that overrides it — on a different L2 requires a `gateway`, the machine's only route to the rest of the cluster, and [`talos.kubespan`](general.md#taloskubespan) enabled: the overlay is what carries the group's pod traffic to the rest of the cluster. A network naming the cluster L2's `cidr` describes the same wire and must agree with it on `mtu` and `vlan`, which every host on one layer-2 network shares (see [MTU](network.md#mtu)).

### `metal.<group>.interfaces`

Optional · mapping of interface name to interface

Each key is a machine's interface name as the OS will see it, and each interface says what the link is for and, where it is statically addressed, with which address. A server may override an interface's settings by name and add interfaces of its own. The merged cabling plan of every machine is checked when the configuration loads, so a machine that could never be joined refuses to load instead of failing at first [`metal join`](../commands.md#metal): exactly one interface carries the `cluster` role and its static address, at most one carries the `external` role, and an `external` role requires a [`network.external`](network.md#networkexternal) block.

### `metal.<group>.interfaces.<name>.role`

Required · `cluster`, `external`, `pxe`, or a list of those

What the link is for: `pxe` is the boot/maintenance link, `cluster` carries the [`network.cluster`](network.md#networkcluster) node network and `external` the [`network.external`](network.md#networkexternal) one. One interface may carry several roles as a list — a NIC that sits on both the cluster and the external network is `[cluster, external]` — with each role named at most once.

The roles decide what the generated machine configuration puts on the link. Every link states `dhcp: false`, so nothing picks up an unexpected lease. A `cluster` link carries its static address and the default route via the group network's gateway; a jumbo group network states the MTU on the link and clamps the route to 1500. An `external` link's configuration rides a VLAN child of the port, created on top of the parent: it carries the machine's anchor address from [`network.external.anchor_cidr`](network.md#networkexternalanchor_cidr) and the routes to the external network, and the machine runs the [return-path static pod](../providers/metal.md) that marks connections entering the child so replies return through the external gateway. A control plane states the API VIP on the link that carries it — its `cluster` link, or the `external` link's VLAN child when [`network.external.kubeapi_vip`](network.md#networkexternalkubeapi_vip) holds the VIP — so a control plane whose VIP rides the external network needs an `external` link, and one without it is refused when the configuration is generated rather than left unable to hold the address. A `pxe` link carries nothing else — it exists so the machine can boot and be reached in maintenance mode.

### `metal.<group>.interfaces.<name>.ip`

Optional · IPv4 address with an optional `/prefix` · default none

The static address of the link. Without a `/prefix` the link network's prefix length is used. On a link carrying both roles the address belongs to the `cluster` side; a dedicated `external` link's address rides its VLAN child. A `cluster` link must carry one — it is the only address the machine is known to answer on — and it must sit inside the machine's own L2, carrying that L2's prefix length when one is written, and must not be the [`kubeapi_vip`](network.md#networkclusterkubeapi_vip) or another machine's address: the loader refuses either collision rather than letting two machines answer for one address.

### `metal.<group>.interfaces.<name>.dns`

Optional · list of IP addresses · default none

The resolvers written into the machine's generated configuration; the first interface that sets them wins, since Talos keeps one resolver list per machine. With none set on any interface, [`network.dns`](network.md#networkdns) is used.

### `metal.<group>.interfaces.<name>.link_name`

Optional · non-empty string · default `<interface>.<vlan>`

The name of the VLAN child link an `external` role creates, instead of the default `<interface>.<vlan>`. It describes that child and nothing else, so an interface without the `external` role is refused for setting it.

### `metal.<group>.interfaces.<name>.vlan`

Optional · integer 1-4094 · default [`network.external.vlan`](network.md#networkexternalvlan)

The VLAN id tagged on an `external` link's VLAN child, instead of the external network's own. Like `link_name`, it is refused on an interface without the `external` role.

### `metal.<group>.boot_timeout`

Optional · seconds · default `600`

How long a machine is given to answer the maintenance-mode apid after it is booted, for both `metal wait`/`metal join` and the converge phase that joins configured machines. Cold hardware can spend many minutes in POST, firmware and NIC initialisation before Talos starts, so a group of slow machines raises this once for every machine in it and a single slow machine overrides it further:

```yaml
metal:
  rack1:
    boot_timeout: 1800      # 30m: these take a long time from cold
    servers:
      srv01:
        boot_timeout: 3600  # and this one longer still
```

A machine that does not answer within its budget is reported and skipped; the rest of the converge is unaffected and the next run picks it up, but the run exits nonzero — the converge is incomplete, not a clean no-op.

### `metal.<group>.bmc`

Optional · mapping

The Redfish settings every machine in the group starts from: `ip` (the BMC's IPv4 address, written bare — no `/prefix`, since it is the host of the Redfish URL and is refused with one), `username`, `password` and `scheme` (the Redfish transport, `https` by default). The credentials are ordinary cluster settings: like every other key they may live in `secrets.yaml` or any included file instead of `cluster.yaml` — where `init --metal` scaffolds them. A [`redfish`](#metalgroupredfish) group must end up with an `ip` and a real `username` and `password` for every machine once each server's overrides merge in — a machine without the address, or with an empty or still-scaffolded `CHANGE-ME` credential, refuses to load.

`https` is the only transport that protects the BMC password, so there is no automatic plaintext fallback: a controller that serves no TLS opts into `http` per machine or group with `scheme`, knowing the credentials then ride the wire unencrypted.

### `metal.<group>.servers`

Optional · mapping of machine name to overrides

Each key names one machine and maps to the settings that machine overrides, which are the group's own keys except `servers`. The name must be a valid hostname, the same name may not appear in two groups, and a machine with no overrides inherits the group as written.
