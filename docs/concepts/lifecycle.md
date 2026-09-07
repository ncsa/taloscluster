# Day 0, day 1, day 2

Everything taloscluster does is one loop: edit `cluster.yaml`, run `taloscluster plan` to see what would change, run `taloscluster converge` to make it so. The same loop covers the first build, growing the cluster and keeping it patched. Command details are in the [README](https://github.com/ncsa/taloscluster#commands).

## Day 0: design

Decide the shape of the cluster before anything exists.

- Pick the provider and how nodes will be reached. See [Machines and access](machines.md) for why tailscale is the default answer on OpenStack and managed Proxmox SDN.
- Pick the Talos and Kubernetes versions. `taloscluster check` works on a fresh directory too and tells you the newest releases, and the Talos [support matrix](https://docs.siderolabs.com/talos/latest/getting-started/support-matrix) says which Kubernetes versions each Talos release supports.
- Size the [pools](../configuration/pools.md). Keep the control plane odd, three for HA. Worker pools can start at zero and grow later.
- Write down who may reach the APIs in [`security`](../configuration/security.md). Include the tailnet.

```bash
taloscluster init --proxmox mycluster   # or --openstack
cd mycluster
vi secrets.yaml cluster.yaml
```

## Day 1: build

```bash
taloscluster plan       # every create, nothing changes
taloscluster converge   # image, network, machines, bootstrap, kubeconfig, health
taloscluster status     # provider resources, endpoints, kubectl get nodes
```

Converge builds the boot image, creates the network and firewall rules, creates the machines, bootstraps etcd on the first control plane, writes `talosconfig` and `kubeconfig` next to `cluster.yaml`, and waits until every node is ready. Installed [plugins](../configuration/rancher.md) run at the end. Back up `talossecrets.yaml` now; it is the cluster's identity and cannot be regenerated.

## Day 2: operate

### Know when something is out of date

```bash
taloscluster check
```

`check` compares the versions pinned in `cluster.yaml` against the newest upstream releases, both the latest patch of the same minor and the latest minor overall, and against what the nodes actually run. It also reports nodes left cordoned. It exits 1 when there is anything to do, so it fits in a cron job or a CI schedule. Use `-o yaml` for machine-readable output.

### See what a change would do

```bash
vi cluster.yaml
taloscluster plan
```

`plan` is converge in dry-run mode. It prints every create, update and delete, the Proxmox sizing changes, the firewall rules that would be added or removed, and the machine-config diff each node would receive, with secrets redacted. Nothing is touched. Read it before every converge on a running cluster.

### Upgrade Talos or Kubernetes

Bump [`talos.version`](../configuration/general.md#talosversion), [`kubernetes.version`](../configuration/general.md#kubernetesversion) or both, then:

```bash
taloscluster plan
taloscluster converge
```

Converge upgrades existing nodes before it adds any new ones, so a new node never joins newer than the rest. Talos goes first when both change. Each node is upgraded in turn and waited for; Talos boots the new image from its second partition and rolls back by itself if it fails. Kubernetes only supports moving one minor at a time, but you do not have to do it by hand: set the version you want and converge fills in the steps. Going from 1.34 to 1.36 upgrades to the newest 1.35 patch first, then to 1.36, with the machine configuration kept at the running version until each hop finishes. Use the patch release `check` suggested for a quiet in-place bump, or jump to the newer minor when you are ready for it.

```yaml
kubernetes:
  version: v1.36.4   # from v1.34.x: converge goes through v1.35.<latest> on its own
```

### Scale

Change a pool's `count` and converge. Raising it adds nodes with the next free numbers. Lowering it drains and removes the highest-numbered nodes, after a confirmation. Add a new pool, for example a GPU pool with its own [extensions](../configuration/pools.md#extensions), the same way.

### Resize or change access

On Proxmox, `cores`, `memory` and a larger `disk` are applied in place; nodes that need a restart to pick them up are listed on every run until `converge --reboot` restarts them one at a time. Editing [`security`](../configuration/security.md) reconciles the security group, the per-VM firewall and the Talos ingress firewall on the next converge. Moving `kubeapi_vip` re-homes the API without a reboot. Renumbering changes, such as another bridge or a different `network.cidr` on a managed SDN, are refused; recreate the cluster instead.

### Look inside

```bash
taloscluster status                # what exists, endpoints, nodes
taloscluster dashboard             # talosctl dashboard on every reachable node
eval "$(taloscluster env)"         # provider CLI credentials for poking around
talosctl --talosconfig talosconfig -n <node-ip> logs kubelet
```

### Tear down

```bash
taloscluster destroy
```

Destroy runs the plugins first, while the cluster is still reachable, then deletes every managed resource and the local `talossecrets.yaml`, `talosconfig` and `kubeconfig`. The shared boot image stays for the next cluster. Remove the nodes from Headscale afterwards if you plan to reuse the name.
