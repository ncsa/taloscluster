# Metal setup

Prepare the machines before joining them to the cluster. For each configuration key, see the [Metal reference](../configuration/metal.md). taloscluster does not provision bare metal: you rack and cable the machines, describe them in a [`metal:` section](../configuration/metal.md) beside the one [OpenStack](openstack.md) or [Proxmox](proxmox.md) section — one VM provider is always required — and join each machine with `taloscluster metal join SERVER`. The join flow itself is described under [Machines and access](../concepts/machines.md#metal); this page is the preparation it needs.

## Network and cabling

Cable every machine to match its group's `interfaces` plan: the `cluster` link carries the machine's static address on the group's L2 (defaulting to `network.cluster`), an optional `external` link rides the external network as a VLAN child, and a `pxe` link exists only to boot and reach the machine in maintenance mode. The plan is checked when the configuration loads — one `cluster` link with its static address per machine, at most one `external` link, an `external` link only beside a `network.external` block — so a cabling description that could never join is refused before anything is planned. Nothing on any link picks up a lease — every link states `dhcp: false` — so the machine is reachable at its cluster address from the first boot, in maintenance mode and after it installs. A machine with an `external` link also runs the generated return-path static pod the [Proxmox external NIC](proxmox.md) uses: it marks connections entering the VLAN child so replies to MetalLB ingress return through the external gateway rather than the default route. The machine running taloscluster must reach the cluster address (the Talos API on port 50000) during the join, and reach the BMC address described below.

A group on an L2 of its own needs [KubeSpan](../configuration/general.md#taloskubespan) enabled — as does a single machine whose server entry overrides the group's `network` with another L2 — and the Kubernetes API VIP must be reachable from the metal L2 — see [what KubeSpan does and does not cover](../concepts/talos.md#one-pod-network-across-networks). The firewalls reach across the L2s on their own: the provider firewall and the Talos ingress firewall on the VM nodes admit the group's L2 and KubeSpan's WireGuard port, and a joined machine runs the same Talos firewall keyed on its own L2 (see [always allowed](../configuration/security.md#always-allowed)); its etcd advertisement follows the same keying, so a control plane in the group advertises the group's L2, the only network holding one of its addresses.

## BMC access

With `redfish: true`, taloscluster talks Redfish to each machine's BMC at `bmc.ip` over https with the group's `username` and `password` — the only transport that keeps the password off the wire, so a BMC that serves no TLS opts into plaintext with [`bmc.scheme: http`](../configuration/metal.md#metalgroupbmc). `init --metal` scaffolds those credentials as `CHANGE-ME` placeholders in `secrets.yaml`, and a `redfish` group refuses to load until every machine has a BMC address and real ones. The BMC is asked for exactly three things: mount the Talos install ISO as virtual media, one-time boot from it, and power the machine on. `metal inspect` also reads a summary of the machine's power state, boot setting, NICs and disks, and `metal eject` unmounts the media.

The BMC is never asked to change BIOS boot modes or the persistent boot order. After the one-time boot the machine falls back to its own boot order, which for an installed machine is its disk, so a machine rebooted with media still mounted boots from the disk. A machine that already answers apid with the cluster's identity is refused by `boot`, `apply` and `join` — the flow would reinstall it, wiping the machine — so configuration changes go through `taloscluster converge`, which pushes the regenerated configuration to the machine's cluster address like any node's (see [Machines and access](../concepts/machines.md)). Converge refuses the two edits a joined machine cannot take in place — a changed install `disk` or cluster-link `ip` — in its validate phase; revert the edit, or reset the machine and join it again. Converge upgrades a joined machine like any node, so a machine joined with an early 0.8.0 development build — whose installer still carried the QEMU guest agent the metal images have since dropped — is reinstalled once, by the first converge after upgrading taloscluster.

A BMC with no internet egress cannot fetch the factory ISO URL itself: run `metal join SERVER --serve` (or `metal boot SERVER --serve`) and the machine running taloscluster downloads the ISO and hands it out over the LAN for as long as the command runs.

## Joining a machine

```bash
taloscluster metal join srv01
```

`join` runs the whole flow for one machine: boot, wait for the maintenance-mode apid on its cluster address, apply the generated machine configuration, eject the media, and verify that the node comes back with its configuration after installing Talos to `disk`. The generated configuration is kept at `.metal/<server>-<role>.yaml` in the cluster directory, mode 0600, because it carries the cluster's credentials; `init` adds `.metal/` to the cluster's `.gitignore`, and `apply` warns when an older cluster directory does not ignore it yet. Every step can also be run on its own (`inspect`, `boot`, `wait`, `apply`, `eject`); see [`metal`](../commands.md#metal) for the syntax.

## Without Redfish

Set `redfish: false` on a group or a single server when the BMC is unreachable, unsupported, or simply not to be touched: taloscluster never talks to that machine's BMC. Boot the machine into Talos maintenance mode yourself — through a PXE server or a USB stick written with the same install ISO — and run `taloscluster metal join SERVER`: it waits for the machine to answer, applies the configuration and verifies it came back with it. `inspect`, `boot` and `eject` skip the machine with a notice. With a PXE server, making an installed machine boot from disk afterwards is your job: the Redfish flow leans on the one-time boot falling back to the machine's own boot order, and PXE has no such fallback.

## Site notes

Hardware- and site-specific observations — how a particular BMC firmware treats mounted media, NIC boot ROM quirks, boot timings — belong in the cluster's own notes, not in these pages: the flow above is the same everywhere, and the quirks are not.
