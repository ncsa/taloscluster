# Quickstart

Create a first cluster, confirm its nodes are ready, and connect with `kubectl`. Complete [Installation](installation.md) first.

## Prepare the provider and access

Choose OpenStack or Proxmox. You need enough capacity for the configured node pools and credentials that can create the provider resources.

- **OpenStack:** prepare an application credential, the identity endpoint, an external network, an availability zone, and the flavor names for your pools. See [OpenStack configuration](configuration/openstack.md).
- **Proxmox:** prepare an API token, VM disk storage, ISO storage, node-local cloud-init storage, and an existing bridge or VNet. See [Proxmox setup](providers/proxmox.md) for token permissions and managed SDN alternatives.

The templates enable Tailscale on the cluster nodes. Before running taloscluster, install and connect Tailscale on the management machine yourself so it is already on the same tailnet; taloscluster does not add that machine automatically. In the cluster configuration, set the login server and supply a pre-auth key that allows all nodes to register. If you omit Tailscale, the management machine must already have routes to the nodes’ real addresses. See [Machines and access](concepts/machines.md#reaching-the-nodes).

## Initialize a cluster directory

Choose one provider:

```bash
taloscluster init --openstack -C mycluster mycluster
# Or: taloscluster init --proxmox -C mycluster mycluster
cd mycluster
```

`-C mycluster` selects the directory to create; the final `mycluster` sets the cluster name. Without `-C`, `init` writes into the current directory.

## Edit the configuration

```bash
vi cluster.yaml secrets.yaml
```

In `cluster.yaml`, replace the provider endpoint, storage or flavor names, network settings, and Tailscale login server with your values. Choose Talos and Kubernetes versions, size the control plane and worker pools, and check the API source allowlists. In `secrets.yaml`, set the matching provider credential and Tailscale pre-auth key. The templates are starting points and require editing before use.

Use three control planes for high availability; one is suitable for a test cluster. Use at least two workers for routine operation, with enough spare capacity to run workloads while one is drained. The generated configuration disables scheduling on control planes. A single worker is suitable only when workload downtime during maintenance is acceptable; see [Worker capacity](concepts/lifecycle.md#worker-capacity). On Proxmox, choose a Kubernetes API VIP outside node addresses and the ingress pool; OpenStack allocates its API VIP and floating IP during converge. Include your management network in both the Talos and Kubernetes API allowlists. See [Planning and building a cluster](concepts/lifecycle.md) for address and port choices, and [Configuration](configuration.md) for every key.

## Review and create

```bash
taloscluster plan
taloscluster converge
```

Review the plan before running converge. Leave plugins inactive until the first cluster and kubeconfig exist if their planning hooks require that kubeconfig; then configure them and review another plan. Converge prepares the image and network, creates the machines, bootstraps the control plane, writes client configuration, waits for healthy nodes, and runs configured plugins.

## Verify and connect

```bash
taloscluster status
kubectl --kubeconfig kubeconfig get nodes
```

Confirm that the expected control planes and workers are present and `Ready`. Use the generated `kubeconfig` for subsequent Kubernetes commands. If nodes do not become ready, start with [Troubleshooting](troubleshooting.md).

Back up `talossecrets.yaml` securely outside the cluster directory. It holds the cluster’s cryptographic identity and cannot be regenerated for a running cluster. Keep `secrets.yaml`, `talossecrets.yaml`, `talosconfig`, and `kubeconfig` out of version control; `init` adds them to `.gitignore`.

Continue with [Usage](usage.md) to scale and upgrade the cluster, or [Commands](commands.md) for the complete CLI reference.
