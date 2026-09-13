# Troubleshooting

Run commands from the cluster directory, or select it with `-C DIR`. Start with `taloscluster status` to inspect resources and endpoints, and `taloscluster plan` to compare configuration with the current cluster.

## Configuration is missing or uses the wrong provider

Check that you selected the directory containing both `cluster.yaml` and `secrets.yaml`. `init NAME` sets the cluster name but does not select a directory; use `init -C DIR NAME` to create files elsewhere. Exactly one provider must be selected, and its credential block must match. See [Configuration](configuration.md).

## A node cannot be reached

Confirm that the VM is running and that the management machine can reach its Talos API on port 50000. With Tailscale, check the login server and pre-auth key and confirm that both the management machine and nodes joined the same tailnet. Without Tailscale, check the route to the real private node addresses.

The [`security`](configuration/security.md) allowlists must permit your management network for both the Talos and Kubernetes APIs. Talos API commands must use real node addresses; the Kubernetes API VIP or floating IP is not a Talos API endpoint. See [Machines and access](concepts/machines.md#reaching-the-nodes).

## Recreating a cluster reuses stale Headscale entries

Before reusing a destroyed cluster’s name, remove its stale nodes from Headscale so reused hostnames resolve to the new machines. Replace `tailscale.auth_key` when it has expired or has already exhausted its permitted uses.

## Proxmox reports missing privileges

The permission preflight reports missing privileges with their ACL paths. Compare the token’s effective permissions with [Proxmox API token permissions](providers/proxmox.md#proxmox-api-token-permissions). Check `Sys.AccessNetwork` on compute-node paths if downloading the boot ISO fails, and SDN privileges when using a managed network.

## Proxmox sizing still shows pending changes

Use `taloscluster plan --reboot`, then `taloscluster converge --reboot`, to review and apply the needed VM restarts. A reboot inside the guest, including a Talos upgrade, does not replace the Proxmox VM process that holds pending CPU and memory settings. Disk growth needs a reboot for Talos to extend its partition; the grow is listed as pending on every run until a `--reboot` converge restarts the node. See [Changing a Proxmox cluster](providers/proxmox.md#changing-a-proxmox-cluster-after-it-exists).

## Managed SDN nodes have no egress or transfers stall

Check FRR, IP forwarding, BGP and VXLAN firewall rules, the exit nodes’ routed uplinks, and VXLAN offload settings against the [managed SDN prerequisites](providers/proxmox.md#managed-evpn-sdn). An available zone alone does not establish that BGP is connected. Resolve other administrators’ pending SDN changes before retrying a run that refuses to apply them.

## `check` exits with status 1

Read the report, or use `taloscluster check -o yaml`. Exit status 1 can mean an available version update, a node running a different version, a leftover cordon, a plugin needing changes, or an error. It is not by itself evidence that the cluster is down. See [`check`](commands.md#check).

## `check` succeeds while version data is unavailable

An upstream lookup failure is a warning, and unknown node versions do not count as drift. A zero exit status therefore does not guarantee that all releases and nodes were checked. Inspect warnings, empty upstream version fields, and the reported node list.

## `check` is incomplete

A check that could not verify everything is reported as incomplete rather than current. The text output warns `check incomplete: <reason>` for each one, and YAML output carries `incomplete: true` with an `incomplete_reasons` list. The known reasons are a newest release (or newest patch of the pinned minor) that could not be fetched from `factory.talos.dev` or `dl.k8s.io`, a running node whose Talos or Kubernetes version is unknown, and a cluster that should exist (a `talosconfig` or `kubeconfig` is present) but answered nothing — `cluster unreachable; no node versions known`. An incomplete check exits `1`, so it never passes a CI gate with unverified data.

Diagnostics: `taloscluster check -o yaml` shows which upstream fields are empty and which node versions are `(unknown)`; the `incomplete_reasons` line names the exact gap. 

Recovery: restore what was missing and re-run `check` until it exits `0`. If the factory or `dl.k8s.io` was unreachable, retry once your network can reach them. If a node's version is unknown, confirm the node is up and reachable on port 50000 and that the management network is in the [`security`](configuration/security.md) allowlist, then re-run. If the cluster answered nothing, investigate why the API is unreachable (see [A node cannot be reached](#a-node-cannot-be-reached)) rather than assuming it is current. See [`check`](commands.md#check).

## Missing Talos secrets

`talossecrets.yaml` holds the cluster CA, the etcd CA, and the join tokens that bind the machines into one cluster. It is the cluster's identity and cannot be regenerated. When a `converge` finds the file missing while the cluster's machines already exist, it refuses to create a fresh identity and fails hard:

```
talossecrets.yaml is missing but 3 machine(s) exist (mycluster-controlplane-01, mycluster-controlplane-02, mycluster-controlplane-03). This file is the cluster's irreplaceable identity (CA + tokens) and cannot be regenerated for an existing cluster -- restore it from backup.
```

A brand-new cluster with no machines still generates the file on its first `converge`, so this message only appears when the identity is genuinely lost. It is not a transient error to retry past.

Recovery: restore `talossecrets.yaml` from your off-machine backup into the cluster directory with mode 0600, then run `taloscluster plan` to confirm the configuration matches what exists and `taloscluster converge` to rebuild the derived `talosconfig` and `kubeconfig`. See [Backup and recovery](backup.md#talos-identity). If the file is truly gone and no backup exists, there is no path back into the existing cluster short of deleting it and rebuilding from `cluster.yaml`.

## A drain fails during scale-down

Lowering a pool's `count` (or removing a control plane during a scale-down) makes `converge` drain the node before resetting and deleting it. If the drain cannot evict everything and the node still reports Ready, converge aborts rather than delete a live node:

```
drain of mycluster-worker-02 failed and node is Ready; aborting to protect a potentially live node
```

Diagnostics say the node is left intact and running; nothing was removed. Find what refuses eviction with `kubectl describe pod` for pod-eviction errors and `kubectl get pdb` for a PodDisruptionBudget that will not go below `minAvailable`. Only a too-tight PDB and a bare (controller-less) pod truly block a drain while the node is Ready; everything else (too few replicas, no eligible node, pinned storage) still evicts and the replacement comes up `Pending`.

Recovery: make the pods evictable, then re-run `taloscluster plan` and `taloscluster converge`; the scale-down retries the drain now that eviction can proceed. Raise `minAvailable`/`maxUnavailable` or `replicas` for a tight PDB, give a bare pod a controller, and add eligible capacity for scheduling or storage. See [Blocked drain](maintenance.md#blocked-drain-what-you-see-and-the-resolution). A drain that fails on a node already `NotReady` is not blocked — converge warns and continues, because there is nothing left to protect.

## An interrupted upgrade leaves a node cordoned

Talos cordons the node it is upgrading and uncordons it when it finishes, but the uncordon is skipped when the upgrade is interrupted, leaving the node `SchedulingDisabled`. The node stays cordoned: nothing schedules onto it, and `talosctl health` fails on it. `taloscluster check` reports the leftover cordon (exiting `1`), and `taloscluster status` marks the node `SchedulingDisabled`.

A control-plane upgrade or reboot interrupted before the node rejoins etcd is more serious. Converge requires an upgraded control plane to pass `talosctl health` — the only signal that it rejoined etcd — before advancing the rollout, and it refuses the kube-api VIP as proof of health. A node that never comes back aborts the rollout rather than move on and risk quorum.

Diagnostics: `taloscluster check` names cordoned nodes with a `SchedulingDisabled` marker and warns that nothing schedules there and `talosctl health` fails on them; `taloscluster status` shows the same marker. For a half-upgraded control plane, `talosctl -n NODE health` fails while the node is still down and the VIP keeps answering (which is not evidence the node rejoined).

Recovery: re-run `taloscluster converge`. It lifts stale cordons on its own (or run `kubectl uncordon NODE` by hand), and it picks the safe reconciliation from wherever the run stopped: it waits a half-done control-plane operation out rather than progress past something that could cost quorum. Investigate with `taloscluster status` first, and re-run converge once the affected node is back. See [Day 2: operate](concepts/lifecycle.md#day-2-operate) and [Backup and recovery](backup.md#recovery-interrupted-bootstrap).

## An installed plugin does not run

Use `taloscluster plugin list` to distinguish a missing package from an unconfigured plugin. Install the needed extra in the same tool environment as taloscluster, then fill in the plugin's configuration and secrets. Running `init` again adds missing plugin sections while preserving existing configuration. See [Plugins](concepts/plugins.md).

## A plugin fails

A plugin failure is contained to that plugin: it is reported, the other plugins still run, and the command's exit code goes nonzero — one plugin cannot take the rest of the run down with it. `converge` and `destroy` report `<name> failed during <hook>: <detail>` as a warning, and `check` turns a plugin's `ok: false` (or an error) into exit status `1`. `status` embeds a plugin's error in its report without changing the exit code. A plugin that cannot be loaded at all is dropped with `plugin <name> could not be loaded (<e>); skipping`. A failed plugin hook does not undo the work `converge` already did to the cluster.

Diagnostics separate the failure classes:

- **Not installed** — `plugin list` omits it, or load warns. Install the extra (`taloscluster[argocd]`, `taloscluster[rancher]`, or `[all]`).
- **Not configured** — `plugin list` marks it unconfigured, and it is skipped (inert until its activation check succeeds). Fill in `cluster.yaml` / `secrets.yaml`.
- **Invalid configuration** — the plugin's `validate` hook refuses a malformed or contradictory section (`plugin <name>: <detail>`) during `plan`/`converge`'s validate phase, before any cluster mutation. Fix the section and re-run.
- **Runtime registration failure** — a `converge` or `destroy` hook error after the cluster was changed (`plugin <name> failed during <hook>: <detail>`). The cluster side of the run is intact; re-run just that plugin with `taloscluster plugin NAME converge`.

Recovery depends on the class above: install, configure, or fix the section, then re-run the affected plugin or the whole converge. A broken plugin is visible in `check`/`status` reports rather than silent, so an `ok: false` names the plugin to fix. See [How plugins run](concepts/plugins.md#how-plugins-run).
