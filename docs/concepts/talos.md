# Why Talos Linux

[Talos Linux](https://www.talos.dev/) is a Linux distribution built for one job: running Kubernetes. There is no shell, no SSH, no package manager and no writable root filesystem. The whole machine is described by a single [machine configuration](https://docs.siderolabs.com/talos/latest/reference/configuration/overview) and managed over a gRPC API with [`talosctl`](https://docs.siderolabs.com/talos/latest/reference/cli), the same way you manage Kubernetes itself with `kubectl`.

taloscluster is a small tool that turns a `cluster.yaml` into a Talos cluster on OpenStack or Proxmox. If you want a supported product for managing many clusters, with a UI, access control and hosted or self-hosted control plane, use Sidero's [Omni](https://www.siderolabs.com/platform/saas-for-kubernetes/) instead. taloscluster is for the case where you want the same Talos benefits on your own cloud with nothing but a git repo and a CLI.

## Small attack surface

Talos ships only what Kubernetes needs: the kernel, containerd, kubelet and a handful of Go services. Because there is no shell or SSH daemon there is nothing to log in to, and the API only accepts mutual TLS with the cluster's own certificates. The root filesystem is read-only and squashfs, the image is signed, and every setting lives in the machine configuration rather than in files someone edited on the host. Anything extra, such as a GPU driver or the guest agent, is added as a signed [system extension](https://docs.siderolabs.com/talos/latest/build-and-extend-talos/custom-images-and-development/system-extensions) baked into the image. taloscluster adds a per-node [ingress firewall](https://docs.siderolabs.com/talos/latest/networking/ingress-firewall) generated from the [`security`](../configuration/security.md) allowlists, so even the Talos and Kubernetes APIs only answer the sources you list. See the [Talos security philosophy](https://docs.siderolabs.com/talos/latest/learn-more/philosophy) for the full argument.

## Upgrades are boring

An [OS upgrade](https://docs.siderolabs.com/talos/latest/configure-your-talos-cluster/lifecycle-management/upgrading-talos) in Talos is `talosctl upgrade` to a new image: the node pulls it, switches the A/B boot partition, reboots, and rolls back on its own if the new system does not come up. A Kubernetes upgrade is `talosctl upgrade-k8s`, which walks the control plane and kubelets through the new version. Neither touches your workloads beyond a normal drain.

With taloscluster both are a one-line edit. Bump [`talos.version`](../configuration/general.md#talosversion) or [`kubernetes.version`](../configuration/general.md#kubernetesversion) in `cluster.yaml`, run `taloscluster converge`, and existing nodes are upgraded one at a time before any new ones are added. Kubernetes moves one minor at a time and Talos goes first when both change. Talos publishes a [support matrix](https://docs.siderolabs.com/talos/latest/getting-started/support-matrix) of which Kubernetes versions each release supports.

## Read more

- [Talos documentation](https://docs.siderolabs.com/talos/latest)
- [What is Talos](https://docs.siderolabs.com/talos/latest/overview/what-is-talos)
- [Omni](https://docs.siderolabs.com/omni), Sidero's multi-cluster management product
- [How machines are created](machines.md) by taloscluster
