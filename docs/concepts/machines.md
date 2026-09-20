# How machines are created and reached

taloscluster discovers managed infrastructure from provider ownership markers rather than a separate infrastructure state file. Keep the local `talossecrets.yaml`, which holds the cluster identity. Cluster resources use deterministic names, while boot images are shared by Talos version and base schematic. Nodes are named `<cluster>-controlplane-01`, `<cluster>-<pool>-01` and so on, which is why adding or removing a node never renumbers the others.

## Boot image

Every converge starts by making sure a boot image for the pinned [`talos.version`](../configuration/general.md#talosversion) and base extension set exists at the provider. The image is built by the [Talos Image Factory](https://factory.talos.dev/) with the base extensions (tailscale and the QEMU guest agent) baked in, and its name embeds the schematic id, so changing the base extension set builds a fresh image under a new identity instead of silently reusing a stale one. Pools that need more, such as GPU drivers, list them under [`extensions`](../configuration/pools.md#extensions); those go into the node's installer image. Proxmox installs that image on first boot. OpenStack starts from the shared boot volume image, so a node converges onto a different extension set through a Talos upgrade after it boots. Converge detects an extension-only change by comparing a node's running schematic (the Image Factory's `schematic` extension reported by `talosctl get extensions`) against cluster.yaml, and after a bootstrap or scale-up asks any node that came up short of its configured extensions to reinstall, so adding or removing an extension reliably takes effect.

The schematic id joined the image name in this release, so the first converge after upgrading builds and uploads a fresh image under the new name while the older `talos-<version>-tailscale` image stays behind. That once-per-cluster rebuild on the new identity is expected; `taloscluster image remove` also deletes the leftover pre-schematic image so it does not linger. Every VM keeps the ISO it was created with on its `ide2` cdrom only until its Talos has installed to disk, after which converge detaches the cdrom (the node boots from `scsi0`), so `image remove` can delete the ISO it used to boot. On Proxmox, `image remove` still refuses to delete any image a cluster-managed VM's cdrom currently references — the detach happens only once converge next passes its health checks. If you need the image gone before that (a node is down or stalled before installing), point at the VM in Proxmox and drop the cdrom by hand with `qm set <vmid> --delete ide2`; the refusal names each VMID still booting it and its slot.

## Machine configuration

For each node taloscluster generates a Talos machine configuration from `cluster.yaml` and the cluster's secrets: hostname, role, node labels, network settings, the Kubernetes API VIP, the ingress firewall, the tailscale key, and any freeform [`config_patches`](../configuration/pools.md#config_patches). On a jumbo layer-2 network the node's link states its MTU and the default route on that link is clamped to 1500, so off-subnet traffic survives a gateway that silently drops jumbo frames; the MTU itself must be agreed by every host on the network — see [MTU](../configuration/network.md#mtu). `taloscluster plan` shows the diff of what would change on a reachable running node. The generated control-plane configuration also installs the kubelet serving certificate approver and metrics-server, and disables workload scheduling on control planes.

## OpenStack

Converge creates a private network from [`network.cluster.cidr`](../configuration/network.md#networkcluster), a router to `external_net`, a security group from the allowlists, and one port per machine. Two extra ports with floating IPs carry the Kubernetes API VIP and the ingress address. Each server boots from the shared image with its machine configuration delivered through the config drive. Servers themselves have no floating IP. See the [OpenStack settings](../configuration/openstack.md).

## Proxmox

Converge downloads the boot ISO to `iso_storage`, writes each node's machine configuration to a small cloud-init volume on node-local storage, creates the VM in the resource pool `taloscluster-<name>`, and configures the per-VM firewall. The first NIC attaches to an existing bridge or VNet, or to a managed EVPN SDN network that taloscluster creates itself. An optional second NIC on a routed subnet can carry the API VIP and ingress addresses directly. After successful health checks, converge detaches and deletes the temporary cloud-init ISO, and detaches the `ide2` boot ISO cdrom now that each node boots from disk. Proxmox keys machines by VM name, so a VM name shared with a cluster-managed machine aborts converge and destroy rather than letting the `cluster/resources` list order hide the managed machine; collisions among unmanaged VMs are ignored. See the [Proxmox settings](../configuration/proxmox.md).

## Metal

Converge does not create bare-metal machines: the [pools](../configuration/pools.md) describe them, you rack, cable and power them, and `taloscluster metal join SERVER` joins each one. Join boots the machine from the Talos install ISO — the same factory image the VM providers boot — by mounting it in the BMC's virtual media and one-time booting from it (`--serve` hands the ISO out over the LAN when the BMC has no internet egress), waits for the maintenance-mode apid on the machine's cluster address, applies the generated machine configuration, ejects the media, and waits for the node to come back with its configuration after installing Talos to `disk`. The BMC is only ever asked to mount media, one-time boot it and manage power: no BIOS boot-mode changes and no boot-order manipulation, so after the one-time boot the machine falls back to its own boot order, which for an installed machine is its disk. A machine whose [`redfish`](../configuration/metal.md#metalgroupredfish) is off is never touched through its BMC: boot it into maintenance mode yourself (PXE, USB) and `join` becomes wait, apply and verify. See [Metal setup](../providers/metal.md) for the preparation the machines need.

## Reaching the nodes

On OpenStack, and on Proxmox with a managed SDN, the nodes sit on a private network with no public address. Only the API VIP and the ingress address are reachable from outside. There is no SSH on Talos anyway, but `talosctl` and `taloscluster` still have to reach the Talos API on port 50000 of a real node address to bootstrap and manage the cluster. The usual answer is a bastion host or a VPN into the tenant network; taloscluster supports two management access paths, and which one applies is decided by whether the `tailscale` section is present in `cluster.yaml`:

- **Tailscale** — the `tailscale` section (even empty) is present, so management talks to the first control plane by its MagicDNS name.
- **Direct** — no `tailscale` section, so management talks to the first control plane's real node address, which your machine must already be able to route to.

Both paths reach the same Talos API on port 50000 of one real node (controlplane-01). The Kubernetes API VIP and the ingress floating IP are not Talos API endpoints; they answer only the Kubernetes API and ingress traffic. Bare-metal machines of a [`metal`](../providers/metal.md) section follow the same two paths: they join the tailnet at boot like any node, and without one they are reached on the static address of their cluster link.

### Path A: an already-connected Tailscale management machine

The [Tailscale](https://tailscale.com/) path is how taloscluster reaches a cluster whose nodes sit on an otherwise unreachable private network. Every node runs the tailscale extension and, given the [`auth_key`](../configuration/tailscale.md#tailscaleauth_key) in `secrets.yaml`, joins your tailnet at boot under its own hostname. You must install and connect Tailscale on the machine you run `taloscluster` from yourself, so it is already on the same tailnet before converge runs; taloscluster does not add that management machine automatically. Pod and etcd traffic stays on the private network; tailscale only carries management traffic.

To use this path end to end:

1. **Connect the management machine** to the tailnet (`tailscale up`, or `tailscale login --login-server=<url>` for Headscale) so it is up before any `taloscluster` run.
2. **Enable the section and an auth key** in `cluster.yaml` / `secrets.yaml`: the `tailscale` section selects Tailscale hostnames for management, and the `auth_key` lets nodes register. See [Tailscale configuration](../configuration/tailscale.md).
3. **Let the allowlists include the tailnet**: add `100.64.0.0/10` to the `kubernetes` and `talos` rules under [`security`](../configuration/security.md), or converge locks itself out of the firewall it just applied. The Talos host firewall opens UDP/41641 for Tailscale whenever the section is present.
4. **Run `taloscluster converge`.** taloscluster reaches the first control plane as `<name>-controlplane-01` by its MagicDNS name, writes the `talosconfig` pointing at that name, waits for a freshly booted node to come up, and bootstraps.

Verify the path with `taloscluster status`, check the endpoint `converge` printed on its `talosctl:` line, and confirm the control plane answers:

```bash
taloscluster status
talosctl --talosconfig talosconfig -n mycluster-controlplane-01 version
```

### Path B: direct access to real node addresses without Tailscale

A cluster without a `tailscale` section works, but only where you can already reach the node addresses — for example Proxmox on a routed bridge, or a routed network your management machine can route to. With no MagicDNS name to resolve, taloscluster falls back to the provider-reported address of the first control plane. The Kubernetes API VIP is never used as this address — it moves between control planes, so it is excluded from guest-agent and Talos discovery address selection, which report the next real address or nothing rather than the VIP.

To use this path end to end:

1. **Make the node addresses reachable**: route the private `network.cluster.cidr` from the management machine — a routed bridge on Proxmox, a router+floating setup on a tenant network, or a VPN. There must be no firewall in the way of TCP/50000.
2. **Omit the `tailscale` section** from `cluster.yaml` and from any file `include` names; a leftover `tailscale.auth_key` in `secrets.yaml` is simply unused, because a `tailscale:` section that only `secrets.yaml` carries does not switch Tailscale on. Removing the section also drops the tailscale extension from new installer images (see [Tailscale](../configuration/tailscale.md)).
3. **Let the allowlists include your management network**: put the source CIDR you reach the node addresses from into the `kubernetes` and `talos` rules under [`security`](../configuration/security.md), or converge locks itself out.
4. **Run `taloscluster converge`.** Without Tailscale, taloscluster resolves the control plane's address in this order: a managed-SDN static address from the network plan, then the address the guest agent reports, polling until a freshly booted node reports one, then the endpoint an earlier `talosconfig` recorded.

Verify the path with `taloscluster status` and confirm the control plane answers on its real address:

```bash
taloscluster status
talosctl --talosconfig talosconfig -n 192.0.2.11 version
```

### Headscale

You do not need a Tailscale account. [Headscale](https://headscale.net/) is a free, open source implementation of the Tailscale control server that you can host yourself; the clients are the unchanged Tailscale clients. Point [`tailscale.login_server`](../configuration/tailscale.md#tailscalelogin_server) at it and generate the pre-auth key there. Before recreating a destroyed cluster with the same name, remove its stale nodes from Headscale so the reused hostnames do not resolve to old machines.
