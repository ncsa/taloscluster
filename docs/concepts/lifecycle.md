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

Always give the Kubernetes API a single stable address of its own rather than pointing clients at a node. taloscluster sets this up on every provider: on OpenStack a reserved port whose fixed address is a Layer 2 VIP shared by the control planes, with a floating IP in front of it; on Proxmox the [`kubeapi_vip`](../configuration/network.md#networkclusterkubeapi_vip), either on the private cluster network or on the routed external subnet. The VIP moves to a healthy control plane when its owner goes away, so kubeconfigs, CI and other clusters never need to learn a new endpoint. Proxmox supports changing its configured VIP later, which moves the API endpoint; OpenStack allocates the addresses through provider ports rather than a configurable VIP key.

### Worker capacity

Use at least two workers so pods have another eligible node during a drain. Size the cluster to carry its workloads with one worker unavailable. A single worker is suitable for a test cluster where maintenance downtime is acceptable; control planes do not accept workloads in the generated configuration.

Node count alone does not ensure availability. Applications need suitable replicas, disruption budgets, and scheduling and storage constraints that permit replacement pods to run elsewhere. A GPU workload needs another eligible GPU worker, for example. See [Kubernetes drain guidance](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/).

### Addresses for ingress

Services of type LoadBalancer need addresses that route to the workers. Install and configure MetalLB or another load balancer separately, or through a configured plugin. The provider setup differs:

- **OpenStack**: a second reserved port and floating IP for ingress, floated onto the workers the same way as the API VIP. Point the MetalLB pool at that fixed address.
- **Proxmox with an external NIC**: [`ingress_pool`](../configuration/network.md#networkexternal), a range on the routed subnet that you must also configure in MetalLB. It enables taloscluster’s ingress return-path configuration and is passed to the ArgoCD plugin, which renders it verbatim as the MetalLB address pool.
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
| everything between nodes on `network.cluster.cidr` (etcd, kubelet, CNI, trustd) | the nodes | taloscluster |
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

Bare-metal machines of a [`metal`](../configuration/metal.md) section are not created by converge: rack and cable them, then join each one with `taloscluster metal join SERVER`. The join boots the machine from the install ISO through its BMC's virtual media — or, with [`redfish: false`](../configuration/metal.md#metalgroupredfish), waits while you boot it into maintenance mode yourself — applies the generated machine configuration, and waits for the node to come back with its configuration after installing Talos to `disk`. See [Metal setup](../providers/metal.md) for the preparation the machines need.

Before it mints a new `talossecrets.yaml`, converge checks whether any of the cluster's machines already exist. If they do, the file is missing and converge aborts instead of creating a fresh identity for a live cluster; restore `talossecrets.yaml` from backup. A brand-new cluster with no machines still generates the file on its first converge.

Whether the cluster needs bootstrapping is decided only by probing the Kubernetes API, and the probe is retried so one transient `kubectl get nodes` failure is never read as a fresh cluster. The signal that a cluster was ever bootstrapped is the `kubeconfig` an earlier converge wrote only after bootstrap completed — but a missing `kubeconfig` is not taken as proof on its own. Because `kubeconfig` is a derived client file (see [backup and recovery](../backup.md)), a recovered management machine restores the Talos identity but not the client file. When machines and the identity exist, converge first regenerates the missing `kubeconfig` from the restored identity, then probes the real cluster. Only a recovery that produces no kubeconfig is read as never-bootstrapped. When a kubeconfig (recovered or not) exists yet the API does not answer after every retry, converge warns loudly that this is an existing but unreachable cluster: it will not recreate the "missing" nodes at the target version, attempt a bootstrap, or run the health phase (all of which need the API), and it skips scale-down, machine-config apply and upgrade. It also defers the mutating plugin converge hooks and exits nonzero — such a converge is incomplete, not a clean no-op — so an unreachable cluster cannot be mistaken for a successful one. Investigate the cluster and re-run once it answers again.

A create that is interrupted before it reaches bootstrap leaves its machines in the inventory but no `kubeconfig`, so the cluster was never bootstrapped at all. The recovery attempt fails there (a never-bootstrapped node serves no kubeconfig), so such an interrupted first run is still read as a fresh cluster: converge attempts bootstrap anyway, even though machines already exist, so it can always self-heal.

Converge runs a validation phase before its first mutation. On Proxmox it compares each existing VM against `cluster.yaml` and refuses changes it cannot reconcile in place — a disk shrink, moving a NIC to another bridge or VLAN, or relocating a VM to a different node or storage — and on OpenStack it refuses a flavor, disk or availability-zone change on an existing server (servers are create-only there). A rejected `cluster.yaml` edit leaves the cluster, its boot image and its Talos configuration untouched. Valid changes, such as growing a disk or resizing cores and memory, still pass through and are applied as before. A Proxmox mutation that completes with warnings (reported as `WARNINGS: N` by the task) is treated as success, so converge and destroy do not abort after the change already took effect.

## Day 2: operate

See [Usage](../usage.md) for checking versions, planning changes, upgrading, scaling, inspecting and destroying a cluster.

### Scaling down

Removals are reconciled from the provider's owned inventory as well as the live Kubernetes node set, so scaling down also deletes machines that never registered as a Node (a worker whose first boot failed) or whose Node a prior run already deleted before the provider VM delete failed — either way the owned VM is not stranded. On OpenStack the network phase also reclaims an owned machine port whose machine is no longer desired and never became a server (its create failed, or the pool shrank before one phase ran) — a port a server-only inventory would otherwise never see and leave orphaned until `destroy`. A stale port whose machine still has a server is left to scale-down, which drains and resets the node before deleting it.

Scaling control planes down is quorum-safe: converge resets each control plane gracefully and, between successive control-plane removals, health-checks the cluster before touching the next. That health check refuses the kube-api fallback — the surviving control planes still serve the VIP even when the removed member never left etcd, so a responding VIP is not proof the cluster is healthy enough to lose another member. A failed or timed-out graceful reset is fatal for a control plane — converge refuses to delete a half-reset etcd member, so a dead member can never cost quorum on a later removal. The same protection covers a control plane whose address is gone: `NotReady` alone does not prove it left etcd, so converge refuses to delete an addressless control plane unless the surviving control plane's authoritative `talosctl etcd members` list confirms it left etcd. That etcd-membership check is also the escape hatch for a control-plane rerun: when the kube Node is already gone but the provider still knows the machine's address (e.g. a wiped/powered-off VM from a prior run whose delete failed), a failed or unreachable reset no longer aborts the scale-down forever — converge treats it as a request for evidence and deletes the VM only once the authoritative etcd-member check confirms the node left etcd, still aborting if it remains a member. Talos discovery service data (`get members`) is not proof of etcd membership — it can be stale and even drops addressless entries — so absence from discovery never licenses a deletion, and scale-down fails closed if the etcd membership query itself cannot be answered.

### Control-plane rollouts

Upgrading or rebooting the control planes is also quorum-safe. After a control plane is upgraded or rebooted, converge requires it to pass `talosctl health` — the only signal that the node rejoined etcd — before advancing the rollout. The kube-api VIP is still served by any surviving control plane, so a responding VIP says nothing about the node being worked on; converge refuses that fallback for a control plane and aborts rather than move on past a member that never came back. The same barrier is restored when a rollout resumes: a control plane already at the target version/schematic is not skipped without re-establishing `talosctl health`, so an interrupted run that died before a node's etcd recovered aborts instead of silently advancing past it.

Every `kubectl` call converge makes is bounded at 30 seconds of wall-clock time, so a kube-api that accepts TCP connections but never answers — such as a floating VIP owned by a control plane that is half-dead during a rollout — cannot hang the run; a retry or `talosctl health` against such an API times out and fails converge. `drain` gets 330 seconds for its own five-minute `--timeout`, and manifest apply/diff/delete get the same headroom so a full apply or a server-side diff against a remote cluster is not cut off as a false timeout. A timed-out call is a safety valve, not a fix: it stops the run instead of hanging it, and leaves investigating the half-dead API to the operator. See [A kubectl request to the kube-api times out](../troubleshooting.md#a-kubectl-request-to-the-kube-api-times-out).

### Config applies

Machine-config applies are also quorum-safe. Converge pushes the fresh config to the control planes one at a time, each settled by default. `apply_config` reports how talos applied it: a silent live/no-op apply (talos reports "without a reboot") costs nothing and never takes a node down, so converge moves straight on; a patch that needs a restart (extra manifests, kubelet args, network) is settled before the next control plane is touched. A restart is detected not by apid answering again (it keeps answering while Talos drains) but by the node going down first; after the node has gone down and its apid answers again, converge still requires the cluster to pass `talosctl health` — the only signal that the node rejoined etcd — before advancing. If a restart-requiring apply never visibly takes the node down within a 120s grace window, converge refuses to advance past a control plane that may never have come back. Workers follow in a single pass.

### Versions

A node scaled up in the same run as a Kubernetes upgrade boots at the upgraded version. Existing nodes get machine configs baked at their running version so `talosctl upgrade-k8s` steps through every minor; a new node has no prior minor to step, so converge regenerates its config at the target version, otherwise it would join one or two minors behind the rest of the cluster until the next converge. The same running-version override retags the Proxmox return-path static pod's kube-proxy image: the provider bakes the target into it, so without the retag that pod would be the one config part that jumps to the target and every node would pull the target image at apply time, before the step-wise upgrade.

Because a running cluster's configs must carry its running version — baking the target into them and applying it would skip every minor in between — converge retries a transient `kubectl version` read and refuses to generate or apply any machine configs if it still cannot establish the cluster's Kubernetes version, rather than risk a config push that skips the upgrade. The one exception is a dry run with no non-empty `kubeconfig` on disk, recovered or not: `plan` writes nothing, so a `plan` after deleting `kubeconfig` next to a reachable cluster cannot read the running version and keeps the target, showing target-version config diffs instead of aborting — a real run writes or recovers the kubeconfig and steps the minors. The same retry-then-abort stance applies during the upgrade itself: the version read and the kube-api stabilization probe are retried a few times like the config-generation read, so a transient hang during the upgrade does not abort converge once the version was already established. Only a persistently unreadable version aborts — after the post-apply stabilization retry, or when no control-plane address resolves, converge refuses to skip the upgrade instead of continuing on an unknown version.
