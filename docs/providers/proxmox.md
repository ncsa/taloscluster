# Proxmox setup

Prepare the provider before following the [Quickstart](../quickstart.md). For each configuration key, see the [Proxmox reference](../configuration/proxmox.md).

## Existing networks

With `bridge` or `vnet` configured, taloscluster uses an existing bridge or VNet and a private Layer 2 Kubernetes API VIP; it does not create Proxmox SDN objects. A minimal provider section is:

```yaml
controlplane:
  count: 3
  cores: 4
  memory: 8 # GB
  disk: 40

proxmox:
  url: https://pve.example.edu:8006
  storage: vms
  iso_storage: isos
  cidata_storage: local
  placement_strategy: spread
  nodes: [pve001, pve002, pve003]
  network:
    cluster:
      bridge: vmbr0
      kubeapi_vip: 10.0.0.240

network:
  cidr: 10.0.0.0/24
  dns: [10.0.0.1]
  ntp: [ntp.example.edu]
```

Use `vnet:` instead of `bridge:` for an existing VNet. A pool may set `node:` to pin all of its machines; otherwise placement spreads creates across the configured online nodes while reserving memory for earlier choices in the same run. `cidata_storage` must be node-local because cidata temporarily contains the Talos machine configuration and provider/extension secrets.

Set `proxmox.url` to the Proxmox server origin; taloscluster adds `/api2/json` internally. Existing configurations that include the API path remain supported.

```yaml
# secrets.yaml
proxmox:
  token_id: taloscluster@pve!provider
  token_secret: CHANGE-ME
```

Operations that load Proxmox inventory perform a read-only `/access/permissions` preflight before a Proxmox upload, VM/pool change, power action, or deletion. Missing privileges are reported with their ACL paths. TLS certificate verification is enabled by default; `proxmox.tls_verify` may name a CA bundle path.

The token also needs `Sys.AccessNetwork` on the node path so Proxmox can fetch the boot ISO with `download-url`; tokens created before this requirement must add it.

## Proxmox directly routed external NIC

`proxmox.network.external` adds a second VirtIO NIC on an externally routed subnet, carrying the Kubernetes API VIP and the MetalLB `ingress_pool`:

```yaml
proxmox:
  network:
    cluster:
      bridge: vmbr0
    external:
      bridge: vmbr0
      vlan: 1691
      cidr: 203.0.113.0/25
      gateway: 203.0.113.1
      anchor_cidr: 169.254.32.0/20
      kubeapi_vip: 203.0.113.79
      ingress_pool: 203.0.113.75-203.0.113.78
```

To expose the Kubernetes API on this NIC, put `kubeapi_vip` in the external section; alternatively, keep it in the cluster section for a private API. Set it in only one section. Each machine gets a deterministic link-local anchor address derived from the cluster and hostname; a collision aborts the run, so size `anchor_cidr` at `/20` or larger rather than reusing a `/24`.

Why the anchor: Talos will not send or receive on an interface without an address, and there is no NAT in this mode, so a NIC that only ever carries a moving VIP still needs one. The `169.254.0.0/16` `/32` is never routed and never advertised; the VIP owner announces the real address on top of it. The same NIC creates an asymmetric-routing problem: a reply to an API or ingress request would otherwise leave through the private default route. Control planes therefore get a dedicated routing table and a source rule for the external API VIP’s `/32` address, and when `ingress_pool` is set every machine also runs a small generated static pod that marks connections entering the external NIC with nftables and restores the mark on replies, so traffic that kube-proxy has reverse-NATed still returns through the external gateway. The routes are native Talos network configuration and the connection-marking rules are installed by the generated static pod after boot.

Enable the Proxmox datacenter firewall yourself; taloscluster only warns when it is disabled. The per-VM Proxmox firewall defaults to deny-in/allow-out and is reconciled on every converge: rules for the current `security:` block are added, stale ones are removed, and duplicates are collapsed. Ownership is by port: the tool reconciles the ports `security:` governs (plus ICMP and intra-cluster traffic) and leaves every other port to you. A rule of yours on tcp/22 is reported and kept; a rule on tcp/6443 is the kube-API allowlist's business and gets reconciled. Generated rules also carry a `taloscluster:` comment so a port you later remove from `security:` still gets cleaned up. Missing rules are added before stale ones are deleted, so editing an allowlist never leaves a port briefly closed. Ports 6443 and 50000 are restricted to the `security.kubernetes` and `security.talos` allowlists, and **80 and 443 are accepted from any source** unless you add an `http:` or `https:` rule — the external NIC is a routable subnet, so ingress is deliberately open by default.

## Changing a Proxmox cluster after it exists

For each desired, owned VM, converge compares CPU, memory, disk size and NIC attachments; `plan` reports supported sizing changes and rejects unsupported attachment changes. Other edits, such as pool placement or VM storage, do not migrate existing VMs:

- `cores` and `memory` are updated in place and take effect when the VM next restarts. Converge lists nodes with pending cores or memory on every run until they restart; `converge --reboot` restarts them for you, one at a time with control planes first and a health check between each. The restart is a Proxmox reboot (an ACPI shutdown Talos handles gracefully, then a fresh start with the new sizing). A reboot from inside the guest, including a Talos upgrade, keeps the old VM process and does not apply pending sizing, so use the flag or stop/start the VM.
- `disk` may only grow: the Proxmox disk is resized online and Talos extends its `EPHEMERAL` partition on the next reboot; use `converge --reboot` in the same run to restart the node, as the grow is not re-listed on subsequent runs. A smaller `disk` is refused before VM sizing and NIC reconciliation, although earlier converge phases may already have run. Revert it, or replace the machine by scaling its pool down past it and back up.
- Moving a NIC to another bridge, VLAN or VNet, adding or removing the `external:` section, or switching `bridge:` to `sdn:` is refused during VM reconciliation before VM sizing changes. Earlier phases can already have applied networking or Talos configuration. These renumber or re-home every node; recreate the cluster instead.
- `security:` edits reconcile the per-VM firewall (see above); `ingress_pool` and `network.ntp` edits flow through the machine config, as do `network.dns` edits on managed SDN, which converge re-applies to every node and `plan` shows as a diff. A `network.cidr` change on a managed SDN cluster is refused because it would renumber running nodes.
- Changing `kubeapi_vip` moves the API endpoint of the running cluster. Control planes are re-applied one at a time (cluster endpoint, certificate SANs, the Layer 2 VIP) and each is waited for before the next, the kubeconfig is regenerated from a control plane, converge waits until the API answers on the new address, and only then are the workers re-applied. Talos applies all of it without a reboot. The old address keeps answering until every control plane has switched, so the move is gradual rather than a hard cutover; still, update anything external that pins the old address. The move is detected against the endpoint recorded in the `kubeconfig` converge wrote, so keep that file next to `cluster.yaml`. The VIP may not sit inside `ingress_pool`.

## Proxmox API token permissions

Create a dedicated user, role and token, then grant the role on the paths the preflight checks. Provider operations validate the token's *effective* permissions before mutation and reports what is missing, so an under-privileged token fails early rather than half-way:

```bash
pveum user add taloscluster@pve
pveum role add TalosCluster -privs "Pool.Allocate Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate Datastore.Audit VM.Allocate VM.Audit VM.PowerMgmt VM.GuestAgent.Audit VM.Config.CDROM VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options SDN.Use Sys.Audit Sys.AccessNetwork"
pveum acl modify / -user taloscluster@pve -role TalosCluster
pveum user token add taloscluster@pve provider --privsep 0
```

The preflight checks the following paths are `/` (`Pool.Allocate`), the ISO, cidata and VM storages, `/vms`, the bridge or VNet at `/sdn/zones/localnetwork` for a bridge or `/sdn/vnets/<vnet>` for a VNet, and every compute node under `/nodes/`; scope the ACL down to those if you prefer. A managed SDN cluster additionally needs `SDN.Allocate` and `SDN.Audit` on `/sdn`. `Sys.Firewall` is not needed: `VM.Config.Network` covers the per-VM firewall.

## Managed EVPN SDN

Replace `bridge:` with an `sdn:` block under `proxmox.network.cluster` to have taloscluster create the private network itself: an EVPN zone, a VNet and an SNAT subnet from `network.cidr`, applied cluster-wide and verified as a bridge on every compute node. Every field is optional:

```yaml
proxmox:
  network:
    cluster:
      sdn:
        # name: phoenix          # zone + VNet id; default cluster name, 2-8 chars, no hyphens
        # controller: evpnctl    # created with peers from the cluster when missing
        # asn: 65000
        # vrf_tag: 12345         # VXLAN ids; default derived from the cluster name
        # tag: 12346
        # exit_nodes: [pve001, pve003]  # default: every node
        # primary_exit_node: pve001
        # mtu: 8950              # underlay MTU minus 50 bytes of VXLAN overhead
        # nodes: [pve001, pve003]       # restrict the zone (and placement) to these
      kubeapi_vip: 192.168.100.2   # or under external: when that section is present

network:
  cidr: 192.168.100.0/24   # anycast gateway at .1, first control plane .11, first worker .61; 50-address pool blocks
  dns: [192.0.2.53]       # required: the overlay has no DHCP
```

Nodes get deterministic static addresses from `network.cidr`, so reordering worker pools would renumber later pools; converge refuses to renumber a running node. Proxmox marks EVPN as a technology preview, and the hosts need preparation that taloscluster cannot do for you:

- FRR installed and running on every node, and `net.ipv4.ip_forward=1` (persist it under `/etc/sysctl.d/`), or the exit node silently drops forwarded traffic.
- With the datacenter firewall on, rules accepting tcp/179 (BGP) and udp/4789 (VXLAN) between the nodes; a zone reports `available` even while BGP sessions sit in `Connect`.
- A routed address on the external uplink on every exit node. An exit node always prefers its own local exit, so a host without one blackholes its VMs' egress; set `exit_nodes` to the hosts that have one.
- VXLAN TX offload disabled on the underlay NIC (`ethtool -K <nic> tx-udp_tnl-segmentation off tx-udp_tnl-csum-segmentation off`, persisted as `post-up`): broken offload lets small packets through and stalls bulk transfers.

When restricting `sdn.nodes`, also restrict `proxmox.nodes` so every eligible compute host belongs to the zone.

The controller is created once and never updated, so a Proxmox node added later must be added to its `peers` by hand. Converge and destroy refuse when another administrator has unapplied SDN changes pending, because applying SDN is cluster-wide. Destroy removes the subnet, VNet and zone it owns and never the controller. Changing `sdn.mtu` later needs a full stop/start of each VM, not a reboot.
