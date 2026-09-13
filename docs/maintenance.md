# Maintenance

This page walks through day-two maintenance of a running cluster: keeping workloads reschedulable while a node is down, what makes a drain succeed or fail, and how to unblock one. It assumes a cluster is running and managed by `taloscluster`; see [Usage](usage.md) for the everyday loop and [Troubleshooting](troubleshooting.md) for diagnosing failures.

Everything here is about one question: *when a node must leave the cluster, can its pods go somewhere else?* The answer depends on how the workloads were laid out, not on `taloscluster`. taloscluster drains a node and carries the workload caps you set into `kubectl`, but it does not decide whether each pod can actually be evicted.

## Spare worker capacity

Your cluster should be able to carry its workloads with one worker unavailable. We recommend at least two eligible workers so a drained node's pods have somewhere to land; a single worker is fine for a test cluster where you accept downtime during maintenance, and control planes do not accept workloads in the generated configuration. This is a recommendation, not an enforcement: a worker pool may have one node (or even zero, to grow later), and `taloscluster` will not refuse to run or drain such a cluster. Plan the extra capacity yourself. See [Worker capacity](concepts/lifecycle.md#worker-capacity).

"Eligible" matters as much as the count. A node that a workload's scheduling rules and storage rules exclude is no help when its current host drains, so sized spare capacity must actually be able to run those pods.

## Replicas

Give deployments enough replicas to survive one node's absence. A `Deployment` with a single replica does not block its own drain (unless a PDB forbids dropping below its floor — see below), but it costs downtime: the Eviction API evicts the running pod, the ReplicaSet creates the replacement on the spot, and that replacement cannot become Ready until the node it lands on accepts it — which, during a drain, may be a node you are not draining yet. With a single replica there is no spare pod running on another node while the original is being drained, because Kubernetes will not create a second pod for one running pod (a `deployment` replica does not multiply itself while a pod is available).

`converge` drains a node when you lower a pool's [`count`](configuration/pools.md#count): it drains, resets, and deletes the node, or drains and removes a control plane during a scale-down. (A `--reboot` restart reboots the node through the provider and lets Kubernetes reschedule its pods, without a draining step.) The drain command evicts pods with `--ignore-daemonsets --delete-emptydir-data`, so DaemonSets and pods using `emptyDir` do not block it, but every other pod must be evictable.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3          # > 1, so one node can be drained while others still run
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: example.com/api:1.0
```

## PodDisruptionBudgets

A `PodDisruptionBudget` (PDB) says how many pods may be voluntarily evicted from a deployment at once. It is the object that most often causes a blocked drain: a `minAvailable` larger than the number of pods that can survive without the node being drained forces kubectl to wait.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api
spec:
  minAvailable: 2        # refuses to evict if fewer than 2 would remain
  selector:
    matchLabels:
      app: api
```

Because a drain is a voluntary eviction, a pod covered by a PDB can only be evicted if the eviction leaves the remaining replica count at or above `minAvailable` (or, for `maxUnavailable`, would not push unavailability past the bound). Raised replicas and a PDB must agree: with `replicas: 2` and `minAvailable: 2`, neither node can be drained, because evicting either pod would drop to 1. Give deployments a spare replica over the PDB floor so one node can leave.

Not everything is subject to eviction, but the set is narrower than it looks. The drain runs with `--ignore-daemonsets --delete-emptydir-data`: pods managed by DaemonSets and mirror or other static pods are skipped rather than evicted (they keep running, and the DaemonSet controller places new ones elsewhere), while pods using `emptyDir` volumes are evicted and their data deleted with them (that flag exists precisely so such pods do not block the drain). The pods a drain truly cannot evict are the ones with no controller at all: a bare pod is not covered by any ReplicaSet to reschedule it, so without `--force` kubectl refuses and errors out rather than leaving it stranded. Every other pod must be evictable, including the ones that autoscalers mark `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` or that use a high `PriorityClass` — the Eviction API checks PodDisruptionBudgets, not those labels, and `kubectl drain` has no such filter. If a node must leave, spare capacity must accept those pods too, or schedule them so their fixed placement does not depend on a node you intend to maintain.

## Scheduling restrictions

Node labels, `nodeSelector`, affinity, and taints decide where a pod is allowed to run. They are the other reason a spare node may not be eligible. A workload pinned to one class of node cannot move to a plain worker even when capacity is free.

Pools declare labels with [`tags`](configuration/pools.md#tags), for example a `workload: gpu` label on a GPU pool. A pod that selects that label can only run on GPU workers, so draining a GPU worker needs *another* GPU worker to take the pod:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: infer
spec:
  replicas: 2
  selector:
    matchLabels:
      app: infer
  template:
    metadata:
      labels:
        app: infer
    spec:
      nodeSelector:
        workload: gpu        # only runs on nodes with this label
      tolerations:
        - key: gpu
          operator: Exists
      containers:
        - name: infer
          image: example.com/infer:1.0
```

Sizing a pool for maintenance means sizing the *eligible* set: a GPU workload needs at least two GPU workers, a workload with an exclusive `node` placement needs a second host the workload can reach, and so on. See the [Worker capacity](concepts/lifecycle.md#worker-capacity) guidance and the drain [recommendation](#spare-worker-capacity) above.

## Storage constraints

A pod using a `PersistentVolumeClaim` can only be evicted if its volume can be attached on another node — but eviction does not check volume attachability, so the drain itself proceeds and it is the relocated pod that must reattach. How the volume is provided decides whether that can happen. A `ReadWriteOnce` (RWO) volume backed by network storage is not pinned to a node: the drain completes and the pod must reattach the volume on whatever eligible node the scheduler picks. A local-path or topology-pinned volume is different: it is bound to a specific node, so the node's departure strands the volume and the replacement pod cannot be scheduled. The eviction itself is not blocked — the Eviction API checks PodDisruptionBudgets, not volume attachability, so a local-path volume does not stop a drain — but the pod is evicted and its replacement goes `Pending`, waiting for a node that can serve the same storage, which no longer exists.

For stateful workloads that must survive a node's departure, use a topology-aware StorageClass (a CSI driver that places replicas on the failover nodes) or run enough replicas with each writing to its own volume so losing one node does not strand the others. Stateless workloads should use remotely attachable or shared storage, or tolerate their data being recreated. See the [Talos documentation](https://docs.siderolabs.com/talos/latest/) and your CSI driver's guides. As with scheduling, the eligible set is what matters: storage must be reachable on the spare node you sized for maintenance.

## Blocked drain: what you see, and the resolution

When you lower a pool's `count` to remove a node (or remove a control plane during a scale-down), `converge` drains the node before it resets and deletes it. A drain that cannot evict everything because of the constraints above fails within `kubectl`'s 5-minute timeout. If the node still reports Ready, converge aborts rather than delete a live, still-serving node:

```
drain of mycluster-worker-02 failed and node is Ready; aborting to protect a potentially live node
```

It tells you the node is left intact and running; nothing was removed. (A drain that fails on a node that is already `NotReady` is not blocked this way — converge warns and continues, because there is nothing left to protect.)

Unblock the drain by making the pods evictable, then re-run converge starting with what actually blocks eviction:

1. Find what is refusing eviction — `kubectl describe pod` for pod-eviction errors and `kubectl get pdb` for a PDB that refuses to go below `minAvailable`.
2. Only two things really block a drain, so check them first:
   - **PDB too tight**: raise `replicas` above the PDB floor, or lower `minAvailable`/raise `maxUnavailable`, so one node can leave.
   - **Bare pod**: a pod not backed by a controller (no ReplicaSet/StatefulSet/etc.) errors the drain without `--force`; give it a controller so it is rescheduled.
3. The rest do not block the drain — the pod is evicted and the replacement comes up `Pending` — but they decide whether the workload survives the node's departure:
   - **Too few replicas**: raise `replicas` so a spare pod is already running on an eligible node while this one drains, instead of downtime while the replacement is recreated.
   - **No eligible node**: add a node the workload's `nodeSelector`/affinity/taints and storage accept, whether by raising the pool's `count` or adding a matching pool, and let it become Ready so the replacement has somewhere to land.
   - **Storage pinned**: make the workload's storage reachable on another eligible node (topology-aware StorageClass, shared/remote volume, or RWO per-replica volumes).
4. Run `taloscluster plan` to review, then `taloscluster converge` again. The scale-down retries the drain now that the pods can be evicted, then resets and removes the node.

A node cordoned for an upgrade (left `SchedulingDisabled` by an interrupted run) blocks scheduling but is a different, gentler condition: `taloscluster converge` lifts those stale cordons on its own, or you can `kubectl uncordon NODE`. See [Troubleshooting](troubleshooting.md) for diagnostics.

## Plan your maintenance

Before opening a maintenance window, confirm the cluster can survive it:

```bash
taloscluster check            # versions, leftover cordons, plugin health
taloscluster status           # endpoints, nodes, who is Ready
kubectl get pods -A -o wide   # where replicas actually run, and how many
kubectl get pdb --all-namespaces
```

A node survives a failed scale-down drain intact, so a blocked maintenance window costs only the run that stalled. Verify each workload's headroom (replicas over PDB floor, an eligible + storage-compatible spare node) before you start, and the drain clears on the next converge.
