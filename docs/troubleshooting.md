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

## Duplicate Proxmox VM names abort converge or destroy

The provider inventory keys Proxmox machines by VM name, so two VMIDs sharing a name that involves a cluster-managed machine are ambiguous and refused before any change:

```
duplicate Proxmox VM names among cluster-managed machines: mycluster-worker-01; the inventory keys machines by name, so rename the VMs so every managed name is unique
```

Diagnostics: both `converge` and `destroy` refuse up front, before mutating anything. Only a collision involving a cluster-managed machine aborts; same-named VMs where neither belongs to this cluster are left alone.

Recovery: rename the VMs so every cluster-managed name is unique, then re-run `taloscluster converge` or `taloscluster destroy`. See [Proxmox](providers/proxmox.md).

## Managed SDN nodes have no egress or transfers stall

Check FRR, IP forwarding, BGP and VXLAN firewall rules, the exit nodes’ routed uplinks, and VXLAN offload settings against the [managed SDN prerequisites](providers/proxmox.md#managed-evpn-sdn). An available zone alone does not establish that BGP is connected. Resolve other administrators’ pending SDN changes before retrying a run that refuses to apply them.

## Destroy refuses while shared SDN controller changes are pending

Teardown never deletes the shared SDN controller, but a pending `deleted` or `changed` state on it would be committed by teardown’s cluster-wide SDN apply and affect other clusters. Destroy (and `plan`) refuse before deleting any VM or the resource pool:

```
refusing to commit pending SDN state on the shared controller controller-01 (changed); teardown never deletes the controller and its staged edits are cluster-wide, so apply or revert them first
```

Diagnostics: destroy aborts before any VM or the pool is deleted; nothing is mutated. `plan` reports the same refusal instead of showing a teardown it would refuse to perform.

Recovery: clear the pending state on the shared controller — apply or revert the staged SDN change — then re-run `taloscluster plan` and `taloscluster destroy`. See [Managed SDN](providers/proxmox.md#managed-evpn-sdn).

## `check` exits with status 1

Read the report, or use `taloscluster check -o yaml`. Exit status 1 can mean an available version update, a node running a different version, a leftover cordon, a plugin needing changes, or an error. It is not by itself evidence that the cluster is down. See [`check`](commands.md#check).

## `check` is incomplete

A check that could not verify everything is reported as incomplete rather than current. The text output warns `check incomplete: <reason>` for each one, and YAML output carries `incomplete: true` with an `incomplete_reasons` list. The known reasons are a newest release (or newest patch of the pinned minor) that could not be fetched from `factory.talos.dev` or `dl.k8s.io`, a running node whose Talos or Kubernetes version is unknown, a configured machine that shows up in neither Talos discovery nor the Kubernetes node list (an existing cluster missing a node), and a cluster that should exist (a `talosconfig` or `kubeconfig` is present) but answered nothing — `cluster unreachable; no node versions known`. An incomplete check exits `1`, so it never passes a CI gate with unverified data.

Diagnostics: `taloscluster check -o yaml` shows which upstream fields are empty and which node versions are `(unknown)`; the `incomplete_reasons` line names the exact gap.

Recovery: restore what was missing and re-run `check` until it exits `0`. If the factory or `dl.k8s.io` was unreachable, retry once your network can reach them. If a node's version is unknown, confirm the node is up and reachable on port 50000 and that the management network is in the [`security`](configuration/security.md) allowlist, then re-run. If a configured machine is missing from both discovery sources, converge it (or scale the pool down in `cluster.yaml` if the node was removed on purpose) and re-run. If the cluster answered nothing, investigate why the API is unreachable (see [A node cannot be reached](#a-node-cannot-be-reached)) rather than assuming it is current. See [`check`](commands.md#check).

## Converge refuses to run against a cluster whose Kubernetes version it cannot read

To generate or apply machine configs, converge must know the running cluster's Kubernetes version, because configs are baked at the running version so an upgrade steps through every minor. When the kube-api is up but will not answer a version query (empty `kubectl version`) after retries, converge aborts before any config mutation:

```
could not determine the running cluster's kubernetes version while the kube-api is up; refusing to generate machine configs against an unknown version. Retry converge or investigate the cluster health.
```

Diagnostics: converge stops before applying any machine config, and no node is changed. The kube-api answers `kubectl get nodes` (via the VIP or a control plane) but `kubectl version` returns nothing.

Recovery: re-run `taloscluster plan` and `taloscluster converge` once the version query works again; if the API stays non-responsive, investigate why it is unreachable (see [A node cannot be reached](#a-node-cannot-be-reached)) rather than converge past an unknown version.

## A kubectl request to the kube-api times out

Every `kubectl` call the core and the rancher and argocd plugins make is bounded at 30 seconds of wall-clock time, so a kube-api that accepts TCP connections but never answers — for example a floating VIP owned by a control plane that is half-dead — cannot hang the run. `drain` gets 330 seconds for its own five-minute `--timeout`, and manifest apply/diff/delete get the same headroom because a full apply or a server-side diff against a remote cluster can legitimately take longer. When a call exceeds its bound, the run fails with a clear error that names the command instead of waiting forever:

```
ERROR: kubectl --kubeconfig /root/kubeconfig get nodes --request-timeout=10s timed out (the api accepted TCP but never answered); investigate the cluster and retry.
```

Diagnostics: the API is reachable at the network layer (the connection is accepted) but not answering kubectl. The operator-facing run exits 1; a version read is retried three times before it aborts, so a transient stall recovers on its own. In the rancher and argocd plugins a timed-out read or write surfaces as that plugin's own `RancherError`/`ApplyError` rather than a raw subprocess traceback, so a hung downstream kube-api is reported as an error instead of being mistaken for "not installed" or for drift.

Recovery: find which control plane owns the floating VIP and confirm it is healthy; if it is half-dead, recover the node (see [A node cannot be reached](#a-node-cannot-be-reached)) or fail over control of the VIP to a healthy control plane, then re-run `taloscluster converge`. The timeout is a safety valve: it does not fix an unhealthy API, only stops the run from hanging on one.

## Converge aborts a Kubernetes upgrade when no control-plane address resolves

After a machine-config apply, converge stabilizes the cluster and re-reads the running Kubernetes version to step the upgrade. If the version stays unreadable and no control-plane address resolves, converge can no longer step upgrades and aborts instead of silently skipping:

```
kubernetes server version unavailable and no control-plane address resolved; cannot perform a kubernetes upgrade
```

Diagnostics: a previous run of a converge that aborted this way exited 0 and silently skipped the upgrade; now it fails, so a skipped upgrade no longer hides behind a healthy exit status. Converge aborts before any `upgrade-k8s` step runs, but machine-config applies and Talos reconciliation earlier in the same run may already have changed or rebooted nodes.

Recovery: investigate why the cluster's control planes are unreachable (see [A node cannot be reached](#a-node-cannot-be-reached)) and ensure a control-plane address resolves, then re-run `taloscluster converge`.

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

Diagnostics say the node is left intact and running; nothing was removed. Find what refuses eviction with `kubectl --kubeconfig kubeconfig describe pod` for pod-eviction errors and `kubectl --kubeconfig kubeconfig get pdb` for a PodDisruptionBudget that will not go below `minAvailable`. Only a too-tight PDB and a bare (controller-less) pod truly block a drain while the node is Ready; everything else (too few replicas, no eligible node, pinned storage) still evicts and the replacement comes up `Pending`.

Recovery: make the pods evictable, then re-run `taloscluster plan` and `taloscluster converge`; the scale-down retries the drain now that eviction can proceed. For a tight PDB, lower `minAvailable` or raise `maxUnavailable` so one node can be evicted (raising `minAvailable` only makes eviction harder), or add replicas above the PDB floor, give a bare pod a controller, and add eligible capacity for scheduling or storage. See [Blocked drain](maintenance.md#blocked-drain-what-you-see-and-the-resolution). A drain that fails on a node already `NotReady` is not blocked — converge warns and continues, because there is nothing left to protect.

## An interrupted upgrade leaves a node cordoned

Talos cordons the node it is upgrading and uncordons it when it finishes, but the uncordon is skipped when the upgrade is interrupted, leaving the node `SchedulingDisabled`. The node stays cordoned: nothing schedules onto it, and `talosctl health` fails on it. `taloscluster check` reports the leftover cordon (exiting `1`), and `taloscluster status` marks the node `SchedulingDisabled`.

A control-plane upgrade or reboot interrupted before the node rejoins etcd is more serious. Converge requires an upgraded control plane to pass `talosctl health` — the only signal that it rejoined etcd — before advancing the rollout, and it refuses the kube-api VIP as proof of health. A node that never comes back aborts the rollout rather than move on and risk quorum.

Diagnostics: `taloscluster check` names cordoned nodes with a `SchedulingDisabled` marker and warns that nothing schedules there and `talosctl health` fails on them; `taloscluster status` shows the same marker. For a half-upgraded control plane, `talosctl --talosconfig talosconfig -n NODE health` fails while the node is still down and the VIP keeps answering (which is not evidence the node rejoined).

Recovery: re-run `taloscluster converge`. It lifts stale cordons on its own (or run `kubectl --kubeconfig kubeconfig uncordon NODE` by hand), and it picks the safe reconciliation from wherever the run stopped: it waits a half-done control-plane operation out rather than progress past something that could cost quorum. Investigate with `taloscluster status` first, and re-run converge once the affected node is back. See [Day 2: operate](concepts/lifecycle.md#day-2-operate) and [Backup and recovery](backup.md#recovery-interrupted-bootstrap).

## Converge refuses to remove a control plane that is still an etcd member

Scaling a control plane down resets each one gracefully. If the graceful reset fails, or the node's address is known but the node is wiped or powered off, converge refuses to delete the machine unless the surviving control plane's authoritative `talosctl etcd members` list confirms the node left etcd:

```
reset of control plane mycluster-controlplane-03 failed (address 192.0.2.30 is known but the node did not reset) and it is still an etcd member (id 1a2b3c4d on control plane mycluster-controlplane-01); refusing to delete a member that could cost quorum
```

An addressless control plane that is merely `NotReady` in Kubernetes is not proof it left etcd either; converge refuses unless the live member list shows it gone.

Diagnostics: the node's VM is left in place; nothing is reset or deleted. This protects quorum — removing a half-reset control plane would let a later removal lose quorum. Talos discovery service data (`talosctl get members`) is not proof of etcd membership and can drop addressless entries, so absence from discovery never licenses a deletion.

Recovery: confirm whether the node actually left etcd. If it is still a member, restore the node or make converge's reset succeed, then re-run `taloscluster converge`; if it is genuinely gone (the surviving control plane's `etcd members` list no longer names it), the rerun deletes the VM on its own. See [Day 2: operate](concepts/lifecycle.md#day-2-operate).

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

## Rancher cannot resolve a configured member

If a configured admin or user cannot be resolved (renamed or removed in Rancher), converge refuses rather than delete that user's existing binding as stale:

```
could not resolve Rancher principals for configured member(s): 'alice' (tier admins); refusing to change memberships so an existing binding is not removed as stale
```

Diagnostics: membership reconciliation stops before any binding changes. `check` reports the unresolved usernames and exits `1` instead of `ok`.

Recovery: fix the netid in `cluster.yaml` so every configured member resolves in Rancher, then re-run `taloscluster converge` or `taloscluster plugin rancher converge`. See [Rancher](concepts/plugins.md).

## Rancher cluster id no longer matches the downstream agent

Two distinct ways the downstream `cattle-cluster-agent` id can disagree with the configured Rancher cluster, with different diagnostics and recovery:

### An unrelated Rancher cluster shares the configured name

A Rancher cluster bearing the configured name still exists, but its id differs from the downstream agent's id — the name was reused for an unrelated cluster (or the agent was re-pointed). Converge refuses to attach to the foreign cluster, and `check`/`status` report the mismatch with `id_match: false` and an `id_mismatch_reason` naming both ids:

```
Rancher cluster 'mycluster' (c-new) does not match the downstream cluster (c-old); converge refuses this registration
```

Diagnostics: `taloscluster check -o yaml` shows `cluster_id`, `downstream_id`, `id_match: false`, and the `id_mismatch_reason`. Members bound to that cluster stay `pending`, and a later `destroy` refuses the cluster the same way rather than deleting a cluster whose id does not match the agent.

Recovery: make the downstream agent and the Rancher cluster agree — either fix the configured cluster name in `cluster.yaml` to match the agent, or remove the unrelated cluster and then clear the now-orphaned agent with the `destroy` step in the next subsection — until `check`/`status` report `id_match: true`, then re-run `taloscluster converge`. See [Rancher](concepts/plugins.md).

### The downstream agent is orphaned (matches no Rancher cluster)

No Rancher cluster bears the configured name at all, but the downstream agent is still registered under its old id — the registration was renamed or deleted in the Rancher UI. Converge refuses to create a fresh import cluster that can never match the stranded agent:

```
the downstream cluster's cattle-cluster-agent is registered as c-old, but no Rancher cluster named 'mycluster' exists; the registration was probably renamed or deleted in the Rancher UI and is now orphaned, so importing a fresh cluster would strand the agent under the old id. The stale Rancher cluster is already gone, so deleting the registration will not clear the downstream agent -- run 'taloscluster destroy' to uninstall the orphaned agent, then re-run
```

Diagnostics: the message above is what converge prints. `taloscluster check -o yaml` shows `registered: false`, `downstream_id`, `id_match: false` and an `orphan_reason` (where otherwise it would report only `registered: false` with no ids), and `taloscluster status` shows the same `downstream_id` and `orphan_reason`:

```
orphan_reason: the downstream cluster's cattle-cluster-agent is registered as c-old, but no Rancher cluster named 'mycluster' exists; the registration was renamed or deleted in the Rancher UI and is now orphaned, so converge refuses to re-register the cluster under a fresh id. The stale Rancher cluster is already gone, so deleting the registration will not clear the downstream agent -- run 'taloscluster destroy' to uninstall it, then re-run
```

Because the stale Rancher cluster is already gone, deleting the registration in the Rancher UI does nothing to the downstream agent, so the old guidance to "delete the stale registration and re-run" deadlocks — converge refuses again because `downstream_rancher_id` still reads `c-old`.

Recovery: run `taloscluster destroy`. With no Rancher cluster bearing the name, destroy recognizes the agent as orphaned and uninstalls it from the downstream cluster (deleting `cattle-system`) instead of printing "not registered; nothing to remove". Then re-run `taloscluster converge` to register the cluster fresh. See [Rancher](concepts/plugins.md).
