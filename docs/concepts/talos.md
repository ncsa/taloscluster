# Why Talos Linux

[Talos Linux](https://www.talos.dev/) is a Linux distribution built for one job: running Kubernetes. There is no shell, no SSH, no package manager and no writable root filesystem. The whole machine is described by a single [machine configuration](https://docs.siderolabs.com/talos/latest/reference/configuration/overview) and managed over a gRPC API with [`talosctl`](https://docs.siderolabs.com/talos/latest/reference/cli), the same way you manage Kubernetes itself with `kubectl`.

taloscluster is a small tool that turns a `cluster.yaml` into a Talos cluster on OpenStack or Proxmox. If you want a supported product for managing many clusters, with a UI, access control and hosted or self-hosted control plane, use Sidero's [Omni](https://www.siderolabs.com/platform/saas-for-kubernetes/) instead. taloscluster is for the case where you want the same Talos benefits on your own cloud with nothing but a git repo and a CLI.

## Small attack surface

Talos ships only what Kubernetes needs: the kernel, containerd, kubelet and a handful of Go services. Because there is no shell or SSH daemon there is nothing to log in to, and the API only accepts mutual TLS with the cluster's own certificates. The root filesystem is read-only and squashfs, the image is signed, and every setting lives in the machine configuration rather than in files someone edited on the host. Anything extra, such as a GPU driver or the guest agent, is added as a signed [system extension](https://docs.siderolabs.com/talos/latest/build-and-extend-talos/custom-images-and-development/system-extensions) baked into the image. See the [Talos security philosophy](https://docs.siderolabs.com/talos/latest/learn-more/philosophy) for the full argument.

taloscluster narrows the surface further. The [`security`](../configuration/security.md) allowlists become three firewalls at once: the provider's (the OpenStack security group or the Proxmox per-VM firewall) and a per-node Talos [ingress firewall](https://docs.siderolabs.com/talos/latest/networking/ingress-firewall), so even the Talos and Kubernetes APIs answer only the sources you list and every port you did not name is closed. The machines can also live on a private network with no public address at all, reachable for management only through [tailscale](machines.md#reaching-the-nodes), a WireGuard-based VPN mesh, while the API VIP and the ingress addresses stay the only things exposed. [Day 0](lifecycle.md#day-0-design) walks through the address and port choices.

## Upgrades are boring

An [OS upgrade](https://docs.siderolabs.com/talos/latest/configure-your-talos-cluster/lifecycle-management/upgrading-talos) in Talos is `talosctl upgrade` to a new image: the node pulls it, switches the A/B boot partition, reboots, and rolls back on its own if the new system does not come up. A Kubernetes upgrade is `talosctl upgrade-k8s`, which walks the control plane and kubelets through the new version. Neither touches your workloads beyond a normal drain.

With taloscluster both are a one-line edit. Bump [`talos.version`](../configuration/general.md#talosversion) or [`kubernetes.version`](../configuration/general.md#kubernetesversion) in `cluster.yaml`, run `taloscluster converge`, and existing nodes are upgraded one at a time before any new ones are added. Kubernetes moves one minor at a time and Talos goes first when both change. Talos publishes a [support matrix](https://docs.siderolabs.com/talos/latest/getting-started/support-matrix) of which Kubernetes versions each release supports.

## Read more

- [Talos documentation](https://docs.siderolabs.com/talos/latest)
- [What is Talos](https://docs.siderolabs.com/talos/latest/overview/what-is-talos)
- [Omni](https://docs.siderolabs.com/omni), Sidero's multi-cluster management product
- [How machines are created](machines.md) by taloscluster
- [Day 0, 1, 2](lifecycle.md) with taloscluster
