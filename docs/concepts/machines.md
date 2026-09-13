# How machines are created and reached

taloscluster discovers managed infrastructure from provider ownership markers rather than a separate infrastructure state file. Keep the local `talossecrets.yaml`, which holds the cluster identity. Cluster resources use deterministic names, while boot images are shared by Talos version. Nodes are named `<cluster>-controlplane-01`, `<cluster>-<pool>-01` and so on, which is why adding or removing a node never renumbers the others.

## Boot image

Every converge starts by making sure a boot image for the pinned [`talos.version`](../configuration/general.md#talosversion) exists at the provider. The image is built by the [Talos Image Factory](https://factory.talos.dev/) with the base extensions (tailscale and the QEMU guest agent) baked in. Pools that need more, such as GPU drivers, list them under [`extensions`](../configuration/pools.md#extensions); those go into the node's installer image. Proxmox installs that image on first boot. OpenStack starts from the shared boot volume image, so a node converges onto a different extension set through a Talos upgrade after it boots. Converge detects an extension-only change by comparing a node's running schematic (the Image Factory's `schematic` extension reported by `talosctl get extensions`) against cluster.yaml, and after a bootstrap or scale-up asks any node that came up short of its configured extensions to reinstall, so adding or removing an extension reliably takes effect.

## Machine configuration

For each node taloscluster generates a Talos machine configuration from `cluster.yaml` and the cluster's secrets: hostname, role, node labels, network settings, the Kubernetes API VIP, the ingress firewall, the tailscale key, and any freeform [`config_patches`](../configuration/pools.md#config_patches). `taloscluster plan` shows the diff of what would change on a reachable running node. The generated control-plane configuration also installs the kubelet serving certificate approver and metrics-server, and disables workload scheduling on control planes.

## OpenStack

Converge creates a private network from [`network.cidr`](../configuration/network.md#networkcidr), a router to `external_net`, a security group from the allowlists, and one port per machine. Two extra ports with floating IPs carry the Kubernetes API VIP and the ingress address. Each server boots from the shared image with its machine configuration delivered through the config drive. Servers themselves have no floating IP. See the [OpenStack settings](../configuration/openstack.md).

## Proxmox

Converge downloads the boot ISO to `iso_storage`, writes each node's machine configuration to a small cloud-init volume on node-local storage, creates the VM in the resource pool `taloscluster-<name>`, and configures the per-VM firewall. The first NIC attaches to an existing bridge or VNet, or to a managed EVPN SDN network that taloscluster creates itself. An optional second NIC on a routed subnet can carry the API VIP and ingress addresses directly. After successful health checks, converge detaches and deletes the temporary cloud-init ISO. See the [Proxmox settings](../configuration/proxmox.md).

## Reaching the nodes

On OpenStack, and on Proxmox with a managed SDN, the nodes sit on a private network with no public address. Only the API VIP and the ingress address are reachable from outside. There is no SSH on Talos anyway, but `talosctl` and `taloscluster` still have to reach the Talos API on port 50000 of a real node address to bootstrap and manage the cluster. The usual answer is a bastion host or a VPN into the tenant network; taloscluster supports two management access paths, and which one applies is decided by whether the `tailscale` section is present in `cluster.yaml`:

- **Tailscale** — the `tailscale` section (even empty) is present, so management talks to the first control plane by its MagicDNS name.
- **Direct** — no `tailscale` section, so management talks to the first control plane's real node address, which your machine must already be able to route to.

Both paths reach the same Talos API on port 50000 of one real node (controlplane-01). The Kubernetes API VIP and the ingress floating IP are not Talos API endpoints; they answer only the Kubernetes API and ingress traffic.

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
talosctl -n mycluster-controlplane-01 version
```

### Path B: direct access to real node addresses without Tailscale

A cluster without a `tailscale` section works, but only where you can already reach the node addresses — for example Proxmox on a routed bridge, or a routed network your management machine can route to. With no MagicDNS name to resolve, taloscluster falls back to the provider-reported address of the first control plane.

To use this path end to end:

1. **Make the node addresses reachable**: route the private `network.cidr` from the management machine — a routed bridge on Proxmox, a router+floating setup on a tenant network, or a VPN. There must be no firewall in the way of TCP/50000.
2. **Omit the `tailscale` section** from `cluster.yaml`; a leftover `tailscale.auth_key` in `secrets.yaml` is simply unused. Removing the section also drops the tailscale extension from new installer images (see [Tailscale](../configuration/tailscale.md)).
3. **Let the allowlists include your management network**: put the source CIDR you reach the node addresses from into the `kubernetes` and `talos` rules under [`security`](../configuration/security.md), or converge locks itself out.
4. **Run `taloscluster converge`.** Without Tailscale, taloscluster resolves the control plane's address in this order: a managed-SDN static address from the network plan, then the address the guest agent reports, polling until a freshly booted node reports one, then the endpoint an earlier `talosconfig` recorded.

Verify the path with `taloscluster status` and confirm the control plane answers on its real address:

```bash
taloscluster status
talosctl -n 192.0.2.11 version
```

### Headscale

You do not need a Tailscale account. [Headscale](https://headscale.net/) is a free, open source implementation of the Tailscale control server that you can host yourself; the clients are the unchanged Tailscale clients. Point [`tailscale.login_server`](../configuration/tailscale.md#tailscalelogin_server) at it and generate the pre-auth key there. Before recreating a destroyed cluster with the same name, remove its stale nodes from Headscale so the reused hostnames do not resolve to old machines.
