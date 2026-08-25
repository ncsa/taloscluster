# Changelog

All notable changes to taloscluster are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- ArgoCD NFS storage uses the CSI provisioner.
- Read package versions from distribution metadata.
- Renamed from `clusterctl` to `taloscluster`.
  - Resources tagged `managed-by=clusterctl` are still discovered.
- `argocd` and `rancher` are plugins of `taloscluster` instead of standalone
  tools; converge, plan, destroy, status and check run them automatically.
- The standalone `argocd` and `rancher` commands are gone; use
  `taloscluster plugin <name>` to run one on its own.

### Fixed

- ArgoCD Taiga NFS shares use the cluster name instead of the OpenStack project.
- ArgoCD cluster values include the Rancher cluster ID when available.
- ArgoCD check detects modified resources as well as missing resources.
- Neutron resources are tagged atomically and conflicting names fail safely.
- OpenStack project lookup handles sessions without an authentication plugin.
- Scale-down and destroy confirm before deleting nodes or plugin-managed resources.
- Converge fails when final Talos and Kubernetes health checks both fail.
- Invalid cluster names, versions, networks and node pools fail before reconciliation.
- The argocd plugin rendered an empty metallb address pool and empty ingress IPs:
  it read them from the `clusterctl` binary, gone since the rename, and ignored
  the failure.

### Added

- Converge Talos Kubernetes clusters on OpenStack from a declarative
  `cluster.yaml`; no state file, resources are discovered via tags.
- Commands: `init`, `plan`, `converge`, `status`, `check`, `dashboard`, `env`,
  `image download` / `image remove`, `destroy`.
- `check` compares pinned versions against upstream and the running nodes;
  exits 1 on an update, drift, or a leftover cordon.
- Converge uncordons nodes left `SchedulingDisabled` by an interrupted upgrade.
- Per-pool Talos extensions and freeform machine-config patches.
- One boot image per talos version, built via factory.talos.dev.
- Tailscale-based node reachability.
- Security-group allowlists for the kube and talos APIs.
- `tags:` in `cluster.yaml` applied as node labels; pool tags win over
  cluster-wide.
- Every node is labeled `ncsa/project` with its OpenStack project.
- `status` also prints the OpenStack endpoint/region/project and the kube-api /
  ingress floating ips.
- `-o yaml` on `status` and `check` for machine-readable output.
- The kube-api and ingress ports join the cluster security group.
- Optional plugins, installed as `taloscluster[argocd]`, `taloscluster[rancher]`
  or `taloscluster[all]`, and inert until configured in cluster.yaml/secrets.yaml.
- `taloscluster plugin list` and `taloscluster plugin NAME [ACTION]`.
- `rancher` plugin: import the cluster, install the agent, reconcile members.
- `argocd` plugin: apply the cluster secret, app project and applications.
- A plugin that fails is reported without stopping the others; the command exits 1.
