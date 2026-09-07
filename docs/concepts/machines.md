# How machines are created and reached

taloscluster has no state file. Every resource is named after the cluster and machine, and tagged, so `taloscluster converge` discovers what exists, creates what is missing, and never touches anything it did not create. Nodes are named `<cluster>-controlplane-01`, `<cluster>-<pool>-01` and so on, which is why adding or removing a node never renumbers the others.

## Boot image

Every converge starts by making sure a boot image for the pinned [`talos.version`](../configuration/general.md#talosversion) exists at the provider. The image is built by the [Talos Image Factory](https://factory.talos.dev/) with the base extensions (tailscale and the QEMU guest agent) baked in. Pools that need more, such as GPU drivers, list them under [`extensions`](../configuration/pools.md#extensions); those land in the node's installer image and apply on its first upgrade pass.

## Machine configuration

For each node taloscluster generates a Talos machine configuration from `cluster.yaml` and the cluster's secrets: hostname, role, node labels, network settings, the Kubernetes API VIP, the ingress firewall, the tailscale key, and any freeform [`config_patches`](../configuration/pools.md#config_patches). `taloscluster plan` shows the diff of what would change on a running node.

## OpenStack

Converge creates a private network from [`network.cidr`](../configuration/network.md#networkcidr), a router to `external_net`, a security group from the allowlists, and one port per machine. Two extra ports with floating IPs carry the Kubernetes API VIP and the ingress address. Each server boots from the shared image with its machine configuration delivered through the config drive. Servers themselves have no floating IP. See the [OpenStack settings](../configuration/openstack.md).

## Proxmox

Converge downloads the boot ISO to `iso_storage`, writes each node's machine configuration to a small cloud-init volume on node-local storage, creates the VM in a resource pool named after the cluster, and configures the per-VM firewall. The first NIC attaches to an existing bridge or VNet, or to a managed EVPN SDN network that taloscluster creates itself. An optional second NIC on a routed subnet can carry the API VIP and ingress addresses directly. See the [Proxmox settings](../configuration/proxmox.md).

## Reaching the nodes

On OpenStack, and on Proxmox with a managed SDN, the nodes sit on a private network with no public address. Only the API VIP and the ingress address are reachable from outside. There is no SSH on Talos anyway, but `talosctl` and `taloscluster` still have to reach the Talos API on port 50000 of a real node address to bootstrap and manage the cluster. The usual answer is a bastion host or a VPN into the tenant network.

taloscluster solves it with [Tailscale](https://tailscale.com/) instead. Every node runs the tailscale extension and, given the [`auth_key`](../configuration/tailscale.md#tailscaleauth_key) in `secrets.yaml`, joins your tailnet at boot under its own hostname. The machine you run `taloscluster` from joins the same tailnet, and the tool reaches the first control plane by its tailscale name, no bastion required. Pod and etcd traffic stays on the private network; tailscale only carries management traffic.

Two things follow from this:

- **The allowlists must include the tailnet.** Add `100.64.0.0/10` to the `kubernetes` and `talos` rules under [`security`](../configuration/security.md), or converge locks itself out of the firewall it just applied.
- **A cluster without tailscale works, but only where you can already reach the node addresses**, for example Proxmox on a routed bridge. Omit the [`tailscale`](../configuration/tailscale.md) section and taloscluster falls back to the provider-reported address.

### Headscale

You do not need a Tailscale account. [Headscale](https://headscale.net/) is a free, open source implementation of the Tailscale control server that you can host yourself; the clients are the unchanged Tailscale clients. Point [`tailscale.login_server`](../configuration/tailscale.md#tailscalelogin_server) at it and generate the pre-auth key there. Before recreating a destroyed cluster with the same name, remove its stale nodes from Headscale so the reused hostnames do not resolve to old machines.
