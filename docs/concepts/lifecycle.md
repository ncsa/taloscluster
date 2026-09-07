# Planning and building a cluster

Everything taloscluster does is one loop: edit `cluster.yaml`, run `taloscluster plan` to see what would change, run `taloscluster converge` to make it so. The same loop covers the first build, growing the cluster and keeping it patched. See [Usage](../usage.md) for everyday operations and [Commands](../commands.md) for syntax and options.

## Day 0: design

Decide the shape of the cluster before anything exists.

- Pick the provider and how nodes will be reached. See [Machines and access](machines.md) for why tailscale is the default answer on OpenStack and managed Proxmox SDN.
- Pick the Talos and Kubernetes versions. `taloscluster check` works before cluster creation once a valid `cluster.yaml` exists and tells you the newest releases, and the Talos [support matrix](https://docs.siderolabs.com/talos/latest/getting-started/support-matrix) says which Kubernetes versions each Talos release supports.
- Size the [pools](../configuration/pools.md). Worker pools can start at zero and grow later.
- Decide the addresses the outside world will use: one for the Kubernetes API and one or more for ingress. See below.
- Write down who may reach the APIs in [`security`](../configuration/security.md). Include the tailnet.

### Control planes and the API address

Run three control planes. One works and is fine for a test cluster, but it has no HA and every upgrade takes the API down for the duration of that node's reboot. etcd needs a majority, so a two-member control plane still cannot tolerate the loss of one member; even counts are warned about. Talos and etcd handle a three-node control plane without any extra load balancer.

Always give the Kubernetes API a single stable address of its own rather than pointing clients at a node. taloscluster sets this up on every provider: on OpenStack a reserved port whose fixed address is a Layer 2 VIP shared by the control planes, with a floating IP in front of it; on Proxmox the [`kubeapi_vip`](../configuration/proxmox.md#kubeapi_vip), either on the private cluster network or on the routed external subnet. The VIP moves to a healthy control plane when its owner goes away, so kubeconfigs, CI and other clusters never need to learn a new endpoint. Proxmox supports changing its configured VIP later, which moves the API endpoint; OpenStack allocates the addresses through provider ports rather than a configurable VIP key.

### Worker capacity

Use at least two workers so pods have another eligible node during a drain. Size the cluster to carry its workloads with one worker unavailable. A single worker is suitable for a test cluster where maintenance downtime is acceptable; control planes do not accept workloads in the generated configuration.

Node count alone does not ensure availability. Applications need suitable replicas, disruption budgets, and scheduling and storage constraints that permit replacement pods to run elsewhere. A GPU workload needs another eligible GPU worker, for example. See [Kubernetes drain guidance](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/).

### Addresses for ingress

Services of type LoadBalancer need addresses that route to the workers. Install and configure MetalLB or another load balancer separately, or through a configured plugin. The provider setup differs:

- **OpenStack**: a second reserved port and floating IP for ingress, floated onto the workers the same way as the API VIP. Point the MetalLB pool at that fixed address.
- **Proxmox with an external NIC**: [`ingress_pool`](../configuration/proxmox.md#ingress_pool), a range on the routed subnet that you must also configure in MetalLB. It enables taloscluster’s ingress return-path configuration, but the ArgoCD plugin currently receives no Proxmox ingress addresses automatically.
- **Proxmox on a plain bridge**: any free range on the cluster network; reachability is whatever the bridge's network provides.

Keep the API VIP out of the ingress range so MetalLB can never hand the API address to a service; the Proxmox configuration rejects an external API VIP inside `ingress_pool`. Separately managed MetalLB pools must be checked by the operator.

### Ports

The [`security`](../configuration/security.md) rules become the OpenStack security group, the Proxmox per-VM firewall and the Talos host firewall. The following describes the generated Talos host rules; provider firewall details and enablement requirements are covered in [Security](../configuration/security.md):

| Port | Who may connect | Set by |
| --- | --- | --- |
| tcp/6443, Kubernetes API | the `kubernetes` rule's hosts | you |
| tcp/50000, Talos API | the `talos` rule's hosts | you |
| tcp/80 and tcp/443, ingress | everyone, until any rule claims the port | you, optional |
| any other port, such as node exporters | hosts of a named rule with an explicit `port` | you, optional |
| everything between nodes on `network.cidr` (etcd, kubelet, CNI, trustd) | the nodes | taloscluster |
| udp/68 DHCP replies | anyone | taloscluster |
| udp/41641 Tailscale | anyone when the `tailscale` section is present | taloscluster |
| ICMP, loopback, established connections, pod/service traffic | Talos built-in exceptions | Talos |

The generated Talos host policy denies other inbound traffic; explicit machine-config patches can change that policy. There is no SSH to open. Both API rules must include the network you run `taloscluster` from, normally the tailnet `100.64.0.0/10`, or converge locks itself out.

```bash
taloscluster init --proxmox -C mycluster mycluster   # or --openstack
cd mycluster
vi secrets.yaml cluster.yaml
```

## Day 1: build

```bash
taloscluster plan       # every create, nothing changes
taloscluster converge   # image, network, machines, bootstrap, kubeconfig, health
taloscluster status     # provider resources, endpoints, kubectl get nodes
```

Converge builds the boot image, creates the network and firewall rules, creates the machines, bootstraps etcd on the first control plane, writes `talosconfig` and `kubeconfig` next to `cluster.yaml`, and waits until every node is ready. Configured [plugins](plugins.md) run at the end. Back up `talossecrets.yaml` now; it is the cluster's identity and cannot be regenerated.

## Day 2: operate

See [Usage](../usage.md) for checking versions, planning changes, upgrading, scaling, inspecting and destroying a cluster.
