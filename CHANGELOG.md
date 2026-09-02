# Changelog

All notable changes to taloscluster are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Make the Talos machine-config generator provider-neutral: each backend contributes its install disk, installer platform, and named machine-config patches.
- Move Proxmox external links, routes, routing rules, `Layer2VIPConfig` and the return-path static pod into the Proxmox backend; move the OpenStack install disk and `eth0` DHCP/VIP patch into the OpenStack backend. Rendered machine configs are unchanged.
- Apply provider patches before user `config_patches`, so an explicit user override still wins.
- Generalize `security` into named rules with an optional `port` and a `hosts` map. `kubernetes`, `talos`, `http` and `https` keep their default ports, and existing `kubernetes`/`talos` name-to-CIDR allowlists load unchanged. A rule name the old loader ignored now needs an explicit `port` instead of being silently dropped.
- Reconcile the Proxmox per-VM firewall on every converge instead of only at VM creation: missing rules are added, stale rules removed, and duplicates collapsed.
- Mark generated Proxmox firewall rules with a `taloscluster:` comment. Rules on the ports `security:` governs are reconciled whether or not they carry the marker, so allowlist removals still close pre-0.5 rules; rules on any other port are reported and left in place.
- Add missing Proxmox firewall rules before deleting stale ones, so editing an allowlist never leaves a port briefly closed.
- Replace a disabled Proxmox firewall rule of ours instead of treating it as satisfying the allowlist.
- Write the Proxmox per-VM firewall policy only when it differs, so a steady-state converge makes no firewall mutation.

### Added

- Restrict tcp/80 with an `http:` rule and tcp/443 with an `https:` rule; a port stays open to all until some rule claims it. `http`/`https` cannot be pointed at another port — name a separate rule for that.
- Reject a provider Talos patch name that is not a plain identifier, so a backend cannot steer patch writes out of the temporary workdir.
- Allow arbitrary named `security` rules (e.g. `metrics: {port: 9100, hosts: {...}}`) on both providers.

## [0.4.0] - 2026-09-01

### Changed

- Move kubeapi_vip to proxmox.network.external when the external section is present; keep it in network.cluster otherwise.
- Select Talos interfaces by deterministic MAC instead of assuming eth0/eth1 naming.
- Require `Sys.AccessNetwork` on the Proxmox node path for ISO `download-url`; existing API tokens must add it before upgrading.

### Added

- Add directly routed external NIC support for Proxmox with a second VirtIO interface and per-VM firewall.
- Generate native Talos v1.13 network config documents (LinkAliasConfig, LinkConfig, DHCPv4Config, RoutingRuleConfig, Layer2VIPConfig) for directly routed external addressing.
- Derive deterministic link-local anchor addresses from cluster and hostname, rejecting collisions.
- Enable Proxmox per-VM firewall with default-deny ingress and default-allow egress.
- Expose the Proxmox ingress pool in provider status for plugin consumption.
- Route Proxmox MetalLB replies through every machine's external NIC with native policy routing and a generated Talos static pod that runs `nft` from the kube-proxy image.

## [0.3.0] - 2026-08-31

### Changed

- Express Proxmox pool memory in GB in `cluster.yaml` and convert it for the API.
- Accept a Proxmox server URL without the `/api2/json` suffix.
- Name Proxmox boot ISOs `talos-<version>-tailscale.iso` like OpenStack images.
- Boot Proxmox VMs with UEFI (OVMF) on q35 instead of legacy BIOS.
- Spread Proxmox control planes across distinct nodes during placement.
- Place control planes by node name instead of available memory so the first node is not systematically skipped.

### Added

- Add Proxmox VM lifecycle support on existing bridges and VNets.
- Add `init --openstack` and `init --proxmox` provider-specific configuration templates.
- Install Talos to Proxmox SCSI disks at `/dev/sda` while retaining `/dev/vda` on OpenStack.

### Fixed

- Stop running Proxmox VMs before deletion so destroy does not fail on a running guest.
- Query live Proxmox VM status before deletion so a VM already shut down by `talosctl reset` is not re-stopped.
- Tolerate drain failure during scale-down so an interrupted run can be resumed.
- Drop `--wait` from `talosctl reset` since `--reboot=false` shuts the node down; waiting for a reboot that never happens hung scale-down for 10 minutes.
- Delete nodes with no resolvable address during scale-down so an already-reset node is not stuck.
- Wait for kube-api to stabilize (two consecutive checks) before Kubernetes upgrade after machine-config apply.
- Skip kubeconfig re-fetch on an already-up cluster and use cp-01 instead of the VIP, which may have moved during a reboot.
- Wait for all desired nodes to become Ready before detaching cidata ISOs so new machines can finish booting.
- Abort scale-down on drain failure when the node is still Ready; only continue if the node is confirmed NotReady.
- Treat kubectl API failure during scale-down as unknown (not NotReady) and abort deletion.
- Re-read Kubernetes server version after kube-api stabilization to avoid skipping minor-version upgrade steps.
- Remove stale swap file and ignore `*.swp` files.

## [0.2.0] - 2026-08-31

### Changed

- Advance the development version to 0.2.0 for Stage 1.
- Route infrastructure lifecycle through a provider backend while preserving OpenStack YAML.
- Accept successful Talos upgrade post-checks when the legacy client exits nonzero.

## [0.1.0] - 2026-08-30

### Changed

- `sync` and `apply` are aliases for `converge`.
- Init adds missing configuration sections for installed plugins.
- ArgoCD monitoring follows `argocd.monitoring.enabled`.
- ArgoCD NFS storage uses the CSI provisioner.
- Read package versions from distribution metadata.
- Renamed from `clusterctl` to `taloscluster`.
  - Resources tagged `managed-by=clusterctl` are still discovered.
- `argocd` and `rancher` are plugins of `taloscluster` instead of standalone
  tools; converge, plan, destroy, status and check run them automatically.
- The standalone `argocd` and `rancher` commands are gone; use
  `taloscluster plugin <name>` to run one on its own.

### Fixed

- Neutron resource creation supports deployments that reject tags in POST.
- ArgoCD Taiga NFS shares use the cluster name instead of the OpenStack project.
- ArgoCD cluster values include the Rancher cluster ID when available.
- ArgoCD check detects modified resources as well as missing resources.
- Neutron resources retain verified ownership tags and conflicting names fail safely.
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
