# Usage

After the [Quickstart](quickstart.md), the normal workflow is to edit `cluster.yaml`, review `taloscluster plan`, and run `taloscluster converge`. This page covers common operations; see [Commands](commands.md) for all options. Use `-C DIR` after the command to work on another cluster directory.

## Know when something is out of date

```bash
taloscluster check
```

`check` compares the versions pinned in `cluster.yaml` against the newest upstream releases, both the latest patch of the same minor and the latest minor overall, and against what the nodes actually run. It also reports nodes left cordoned. It exits 1 for detected updates, drift, cordons, or unsuccessful plugin checks, so it fits in a cron job or a CI schedule. Use `-o yaml` for machine-readable output. Unavailable upstream or node-version data can still yield exit status 0; inspect warnings and empty fields before treating the report as complete.

## See what a change would do

```bash
vi cluster.yaml
taloscluster plan
```

`plan` is converge in dry-run mode. It prints every create, update and delete, the Proxmox sizing changes, the firewall rules that would be added or removed, and the machine-config diff each node would receive, with secrets redacted. Provider and cluster resources are unchanged, though planning can register idempotent schematics with the Talos Image Factory. Read it before every converge on a running cluster.

## Upgrade Talos or Kubernetes

Bump [`talos.version`](configuration/general.md#talosversion), [`kubernetes.version`](configuration/general.md#kubernetesversion) or both, then:

```bash
taloscluster plan
taloscluster converge
```

Converge upgrades existing nodes before it adds any new ones, so a new node never joins newer than the rest. Talos goes first when both change. Each node is upgraded in turn and waited for; Talos boots the new image from its second partition and rolls back by itself if it fails. Kubernetes only supports moving one minor at a time, but you do not have to do it by hand: set the version you want and converge fills in the steps. Going from 1.34 to 1.36 upgrades to the newest 1.35 patch first, then to 1.36, with the machine configuration kept at the running version until each hop finishes. Use the patch release `check` suggested for a quiet in-place bump, or jump to the newer minor when you are ready for it.

```yaml
kubernetes:
  version: v1.36.4   # from v1.34.x: converge goes through v1.35.<latest> on its own
```

## Scale

Change a pool's `count` and converge. Raising it adds nodes with the next free numbers. Lowering it drains and removes the highest-numbered nodes, after a confirmation. Add a new pool, for example a GPU pool with its own [extensions](configuration/pools.md#extensions), the same way. Extensions activate on install or upgrade; review the [extension limitations](configuration/general.md#talosextensions) and verify what the nodes actually run.

## Resize or change access

On Proxmox, `cores`, `memory` and a larger `disk` are applied in place; pending CPU and memory changes are listed on every run until `converge --reboot` restarts them one at a time. Disk-growth restart requirements are reported only in the run that grows the disk, so include `--reboot` in that run. Editing [`security`](configuration/security.md) reconciles the security group, the per-VM firewall and the Talos ingress firewall on the next converge. Moving `kubeapi_vip` re-homes the API without a reboot. Renumbering changes, such as another bridge or a different `network.cidr` on a managed SDN, are refused; recreate the cluster instead.

On OpenStack, `flavor`, `disk`, and `availability_zone` affect new servers only; converge does not resize or move existing servers. See [Network](configuration/network.md) for provider-specific DNS behavior.

## Look inside

```bash
taloscluster status                # what exists, endpoints, nodes
taloscluster dashboard             # talosctl dashboard on every reachable node
eval "$(taloscluster env)"         # provider CLI credentials for poking around
talosctl --talosconfig talosconfig --endpoints mycluster-controlplane-01 --nodes 192.168.0.10 logs kubelet
```

The Talos example uses controlplane-01’s Tailscale name as the endpoint and the node’s private address as the target. Replace both with your cluster’s values; without Tailscale, use controlplane-01’s reachable real address for the endpoint.

## Tear down

```bash
taloscluster destroy
```

Destroy runs the plugins first, while the cluster is still reachable, then deletes every managed resource and the local `talossecrets.yaml`, `talosconfig` and `kubeconfig`. The shared boot image stays for the next cluster. Remove the nodes from Headscale afterwards if you plan to reuse the name.
