# taloscluster

`taloscluster` provisions and manages [Talos Linux](https://www.talos.dev/) Kubernetes clusters on OpenStack or Proxmox. It combines provider resource management, Talos configuration, and Kubernetes lifecycle operations in one command-line tool.

Describe the cluster you want in `cluster.yaml` and keep provider credentials and other secrets in `secrets.yaml`. Run `taloscluster plan` to review changes, then `taloscluster converge` to apply them. The same workflow creates a cluster, changes its size and access rules, and performs rolling Talos and Kubernetes upgrades.

## What it manages

- Provider boot images, networks, firewall rules, and machines for control plane and worker pools.
- Talos machine configuration, cluster bootstrap, and generated `talosconfig` and `kubeconfig` files.
- Scaling and rolling version upgrades, with existing nodes upgraded before new nodes are added.
- Talos bootstrap manifests for metrics-server and the kubelet serving certificate approver, plus optional [plugins](concepts/plugins.md) for Rancher registration and ArgoCD integration.

Resources have deterministic names and ownership tags, so taloscluster discovers existing infrastructure without a separate infrastructure state file. The cluster directory still holds configuration, credentials, and the generated Talos cryptographic identity; see [Configuration](configuration.md#other-files-in-the-directory) for the files to retain and back up.

## How it fits together

Talos Linux runs Kubernetes on the nodes and exposes an API for managing the operating system. taloscluster uses that API through `talosctl`, manages Kubernetes through `kubectl`, and talks to the provider to create and reconcile infrastructure. You continue to use Kubernetes tools to manage your workloads. See [Why Talos](concepts/talos.md) for background and [Machines and access](concepts/machines.md) for networking and node access.

## Get started

Start with [Installation](installation.md), then follow the [Quickstart](quickstart.md) to create and verify your first cluster. For an existing cluster, use [Usage](usage.md) for common operations, [Commands](commands.md) for CLI details, and [Configuration](configuration.md) for settings. [Troubleshooting](troubleshooting.md) covers common failures.
