# Changelog

All notable changes to taloscluster are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add the `charts` plugin (`taloscluster[charts]`): install Helm charts and manifests (Gateway API, MetalLB, Traefik, cert-manager, sealed-secrets, NFS and Ceph CSI) into the cluster during converge, drift-driven, with the `charts:` section (including the `charts.ceph` credentials) read from the merged configuration.
- Add `show_yaml`/`redact` to `taloscluster.output` so plugin dry-run previews mask credential-looking keys and every value of a Kubernetes Secret.
- Join a bare-metal machine missing from the cluster during converge only when its group sets `auto_join: true`, never reinstalling one that already answers apid with the cluster's identity.
- Warn during `check` and `converge` when a configured host network overlaps the Kubernetes pod (`10.244.0.0/16`) or service (`10.96.0.0/12`) network, which is what Talos's `address-overlap` diagnostic reports on a node.
- Add `metal.<group>.boot_timeout` (seconds, default 600) for how long a machine may take to reach maintenance mode, overridable per server, for hardware that is slow from cold.
- Add `metal` commands that inspect, boot, wait, apply, eject and join bare-metal machines, refusing an already-joined machine and skipping the BMC when redfish is disabled. `metal apply` generates the machine config against the cluster endpoint the provider resolved — refusing until converge has run — writes it to `.metal/` (which `init` git-ignores) at mode 0600 and warns when the directory is not ignored.
- Accept a `metal` section defining bare-metal machine groups beside one required VM provider, requiring real BMC credentials for `redfish` groups (https-only unless `bmc.scheme` opts into http) and refusing cabling, BMC and network settings that could never join, such as a `/prefix` on a BMC address or a control plane with no external link while the kubeapi VIP rides the external network.
- Add `bmc.tls_verify` to verify Redfish TLS against the system trust store or a pinned CA bundle; BMC certificates stay unverified by default.
- Treat metal machines as cluster nodes throughout: they join at the cluster's running Kubernetes version and the tailnet when tailscale is configured, get the same firewall as VMs with every group's L2 and KubeSpan's UDP port admitted, count as desired nodes in scale-down and `check`, and scale down into maintenance mode keeping the Talos install so the machine can join another cluster.
- Add `--metal` to `init` to scaffold the bare-metal section and its BMC credentials beside a provider.
- Add `link_name` and `vlan` overrides to metal interfaces for the generated external VLAN child link and its ingress return-path pod.
- Add `talos.kubespan` (default false) enabling Talos KubeSpan, sized to the L2 MTU, advertising every node address except the external network's, and required for metal machines on another L2.
- Document the KubeSpan reachability contract for clusters spanning layer-2 networks, including the API VIP and proxy requirements.
- Document the MTU rules for jumbo layer-2 networks, including the jumbo-frame ping recipe.
- Document the metal provider with a setup guide, join-flow coverage for both the `metal join` command and converge's `auto_join` path, and a boot-media troubleshooting entry.
- Add a top-level `include` list that merges extra YAML files into `cluster.yaml` before validation, refusing `cluster.yaml` itself, nested includes and a value set in two files.
- Bound every `kubectl` call with a request timeout so a hung kube-api fails converge.
- Report a machine missing from both Talos discovery and Kubernetes as an incomplete `check`.
- Exit nonzero from `check` when version data is incomplete, with `incomplete` and `incomplete_reasons` in the report.
- Detect extension-only changes from the running schematic so adding or removing an extension reinstalls the node.
- Refuse OpenStack flavor, disk and availability-zone changes with recreation guidance; update an existing subnet's DNS in place.
- Deny pods in the default namespace the OpenStack metadata service with a NetworkPolicy shipped in the bootstrap manifests.
- Warn that `network.dns` is not applied on DHCP-backed Proxmox networks.
- Create and reconcile Proxmox VM NICs inheriting the bridge MTU (a running VM's NIC is rewritten at its restart, since a live re-plug drops flannel's VXLAN device), and warn when the cluster or external bridge is below it.
- Refuse duplicate Proxmox VM names that involve a cluster-managed machine.
- Refuse Proxmox SDN teardown or converge while the shared controller or the cluster's own zone, VNet or subnet has pending `deleted` or `changed` state, before any VM is deleted.
- Report the firewall a new Proxmox VM would get during `plan`.
- Print one `ERROR:` line and exit 1 on provider API failures instead of a traceback.
- Refuse unknown or misspelled keys in every fixed-schema section of `cluster.yaml` and `secrets.yaml`, including plugin and nested Proxmox sections.
- Refuse null, non-string, empty or scaffolded `CHANGE-ME` secrets when a command needs the credential.
- Run plugin validation for every installed plugin before any cluster mutation, including standalone `plugin NAME` commands.
- Report plugin work that must wait for the first bootstrap as deferred in `plan` instead of failing.
- Require the cluster name to confirm `plugin NAME destroy`, matching top-level `destroy`.
- Validate Rancher settings and require the `BackingNamespaceCreated` condition to be `True` before creating registration tokens.
- Split ArgoCD sync settings: `argocd.sync` sets the chart value, new `argocd.automated` controls automated sync, pruning and self-healing.
- Require real YAML types for ArgoCD booleans, member lists, URLs and apply targets; refuse unknown per-app keys and version overrides on apps that ignore them.
- Add guides for OpenStack setup, backup and recovery, node maintenance, load balancers and ingress, and both management access paths.
- Expand troubleshooting with diagnostics and recovery for the new refusals, missing Talos secrets, failed drains, incomplete checks, plugin failures and interrupted upgrades.

### Changed

- **Breaking:** require Proxmox 9 or newer, refused during converge's validate phase: VM NICs rely on Proxmox 9 inheriting the bridge MTU from an unset MTU, which Proxmox 8 does not do.
- Refuse a Proxmox SDN zone MTU below the cluster MTU.
- **Breaking:** merge `secrets.yaml` into the cluster configuration through the `include` list (the scaffold lists it), so credentials — plugin ones included — can live in any included file; a `cluster.yaml` that does not include it no longer reads it.
- **Breaking:** the network settings, including a new `mtu` applied to links and the default route, move into `network.cluster` and `network.external`; the old address keys are refused ([old-to-new key table](docs/configuration/network.md#moving-from-the-old-keys)).
- Delete the legacy `talos-<version>-tailscale` image on `image remove`, refusing while a managed VM still boots it, and converge detaches the boot ISO cdrom once a node boots from disk.
- Name the timed-out kubectl command in timeout errors and allow manifest apply, diff and delete more time than a probe.
- Apply machine configs to control planes one at a time, waiting for each restart to finish.
- Require `talosctl health` after a control-plane upgrade, reboot or config apply before touching the next one; the kube-api VIP no longer counts as healthy.
- Remove owned machines that never joined Kubernetes, or whose VM delete failed earlier, during scale-down.
- Refuse to generate machine configs when a running cluster's Kubernetes version cannot be read.
- Refuse a Kubernetes upgrade when the running version cannot be determined instead of skipping it.
- Retry a hung kube-api read during the upgrade phase instead of aborting converge.
- Retry the kube-api probe and warn when an existing cluster is unreachable instead of treating it as new; converge then exits 1 and defers plugin changes.
- Recover a missing kubeconfig from the Talos identity before deciding a cluster is new, on clusters with or without Tailscale.
- Refuse to generate a fresh `talossecrets.yaml` when machines already exist.
- Never use the kube-api VIP as a node address; nodes reporting only VIPs fall back to the provider inventory.
- Reject unsupported Proxmox changes (disk shrink, NIC move, placement or storage change) before any mutation, and report them in `plan`.
- Re-verify the managed SDN bridge on every converge, waiting up to a minute for it to appear.
- Use the configured OpenStack region instead of a hardcoded `RegionOne`.
- Include the extension schematic in the boot image and ISO name so changing base extensions builds a fresh image.
- Normalize a missing `v` prefix on `talos.version` and `kubernetes.version`.
- Scaffold the Proxmox `kubeapi_vip` outside the managed-SDN static layout.
- Create `talossecrets.yaml` with mode 0600 from the start.
- Forward plugin `check`/`status` results to later plugins so ArgoCD renders the same Rancher cluster id as converge.
- Refuse Rancher converge and destroy on a downstream-agent id mismatch; `check`/`status` report it and `plugin rancher destroy` removes an orphaned agent.
- Resolve Rancher members on an exact id match, and reject a user listed under both `admins` and `users` or under both via an alias.
- Render ArgoCD manifests through a YAML serializer so values with quotes, colons or newlines are preserved.
- Deliver the OpenStack Cinder cloud.conf as a Secret instead of embedding credentials in ArgoCD values.
- Activate the ArgoCD plugin only with a `kubeconfig` or `context` apply target.
- Reorganize the documentation around installation, quickstart, usage, commands, configuration, plugins and troubleshooting, and shorten the README.
- Reinstall metal machines joined with an early 0.8.0 development build once, on the first converge after upgrading taloscluster.

### Fixed

- Treat a truncated or hand-edited `kubeconfig` or `talosconfig` as having no recorded endpoint instead of crashing converge.
- Parse the `talosctl etcd members` table by column offset so a member with an empty hostname fails closed during scale-down.
- Refuse to delete a control plane during scale-down unless the surviving control planes confirm it left etcd.
- Refuse to remove a node dropped from the config during scale-down while etcd membership is unreadable.
- Abort a control-plane scale-down when the graceful reset fails or times out, and health-check between removals.
- Boot VM and bare-metal nodes added in the same run as a Kubernetes upgrade at the upgraded version.
- Detect Tailscale addresses across the whole `100.64.0.0/10` range.
- Redact registry passwords, `machine.files` and inline-manifest contents, multiline credentials and secret-like environment entries from the `plan` diff.
- Emit no `--login-server` argument when `tailscale.login_server` is unset.
- Fall back to the control plane's real address when a tailscale section has no auth key instead of hanging converge.
- Refuse to switch Tailscale on or off for a running cluster, which would deadlock the node rollout.
- Remember a grown Proxmox disk until the node reboots so a later `--reboot` converge restarts it.
- Accept Proxmox tasks that finish with warnings as successful.
- Reclaim OpenStack ports left behind by machines that never got a server.
- Fix OpenStack security-group normalization so allowlists are enforced and `0.0.0.0/0` is not recreated on every run.
- Fix the hostname-length check for pools with 100 or more nodes.
- Contain a plugin's fatal error so one plugin cannot abort the whole run; warn on duplicate plugin entry-point names.
- Fail Rancher member reconciliation when a configured user cannot be resolved instead of removing their binding.
- Fix Rancher API error messages that were joined character by character.
- Pass the Proxmox `ingress_pool` to ArgoCD so MetalLB address pools render for both providers.
- Give the ArgoCD AppProject `user` role the read access its name implies.
- Reconfigure and upgrade joined metal nodes during converge, and warn that destroy leaves them running the destroyed cluster.
- Advertise etcd on a metal control plane's own L2 instead of the cluster network.
- Give metal nodes the cluster-wide `tags:` and the provider default node labels the VM nodes get.
- Refuse a `talos.version` older than the running release and a `kubernetes.version` the pinned Talos does not support.
- Wait out control-plane reboots through the control-plane endpoint so an unroutable node address no longer stalls the rollout.
- Generate metal machine configs with the same hostname document as VM nodes on every supported Talos version.
- Refuse a joined metal machine's changed `disk` or cluster address during validate, before any phase mutates.
- Create the `talosconfig` and scaffolded `secrets.yaml` with mode 0600 from the start.

## [0.7.0] - 2026-09-06

### Added

- Reconcile Proxmox `cores`, `memory` and `disk` in place; `converge --reboot` restarts nodes whose sizing waits on a restart.
- Refuse unsupported Proxmox changes (disk shrink, NIC move, adding or removing `external:`) before any mutation.
- Move the kube-api VIP of a running cluster when `kubeapi_vip` changes.
- Reject a `kubeapi_vip` inside `ingress_pool`.
- Show the redacted machine-config diff each node would receive under `plan`.
- Address control plane 01 by its real address instead of the VIP when Tailscale is off, including during a VIP move.
- Add a `docs/` configuration reference and an MkDocs site published to GitHub Pages.
- Document Proxmox API token permissions, managed SDN prerequisites and post-creation `cluster.yaml` changes.

### Changed

- Configure `argocd.infra.url` and `argocd.nfs.servers` in `cluster.yaml` instead of hardcoding them; docs use placeholder addresses.
- Require `talos.version` v1.13.0 or newer.
- Generate network settings as Talos network config documents instead of the deprecated `machine.network` fields.
- Apply the `security:` allowlists as the Talos ingress firewall on every node.
- Upgrade Kubernetes one minor at a time with `talosctl upgrade-k8s`; refuse a version older than the running cluster.
- Retry `talosctl bootstrap` for up to five minutes while etcd is not ready yet.

## [0.6.0] - 2026-09-03

### Added

- Add managed Proxmox EVPN SDN via `proxmox.network.cluster.sdn` with a zone, VNet, SNAT subnet and shared controller; nodes get deterministic static addresses.
- Refuse unrelated pending SDN changes and same-named foreign zones or VNets.
- Require `SDN.Allocate` and `SDN.Audit` in the Proxmox permission preflight when SDN is managed.
- Work without Tailscale: resolve node addresses from the provider and omit the tailscale extension when `tailscale:` is unset.
- Skip the kube-api VIP when picking node addresses from Talos discovery.

### Changed

- Destroy also removes the owned SDN subnet, VNet and zone, never the controller.

## [0.5.0] - 2026-09-01

### Changed

- Make the machine-config generator provider-neutral; each backend contributes its own patches, applied before user `config_patches`.
- Generalize `security` into named rules with a `port` and a `hosts` map; existing `kubernetes` and `talos` allowlists load unchanged.
- Reconcile the Proxmox per-VM firewall on every converge, adding rules before removing stale ones and leaving unmanaged ports in place.

### Added

- Restrict tcp/80 and tcp/443 with `http:` and `https:` rules, and allow arbitrary named `security` rules on both providers.

## [0.4.0] - 2026-09-01

### Changed

- Move `kubeapi_vip` to `proxmox.network.external` when that section is present.
- Select Talos interfaces by MAC address instead of assuming interface names.
- Require `Sys.AccessNetwork` on the Proxmox node path for ISO downloads.

### Added

- Add a directly routed external NIC for Proxmox with a per-VM firewall, policy routing and a return-path static pod for MetalLB replies.
- Expose the Proxmox ingress pool in provider status for plugins.

## [0.3.0] - 2026-08-31

### Changed

- Express Proxmox pool memory in GB and accept a server URL without `/api2/json`.
- Boot Proxmox VMs with UEFI on q35 and spread control planes across distinct nodes.

### Added

- Add Proxmox VM lifecycle support on existing bridges and VNets, with `init --proxmox` and `init --openstack` templates.

### Fixed

- Stop running Proxmox VMs before deletion and skip VMs already shut down by `talosctl reset`.
- Abort scale-down when a drain fails on a node that is still Ready or whose state is unknown; continue only when it is NotReady.
- Wait for kube-api to stabilize before a Kubernetes upgrade and re-read the server version so no minor step is skipped.
- Wait for all nodes to become Ready before detaching cidata ISOs.

## [0.2.0] - 2026-08-31

### Changed

- Route the infrastructure lifecycle through a provider backend.
- Accept successful Talos upgrade post-checks when the legacy client exits nonzero.

## [0.1.0] - 2026-08-30

### Changed

- Rename from `clusterctl` to `taloscluster`; resources tagged `managed-by=clusterctl` are still discovered.
- Make `argocd` and `rancher` plugins run by converge, plan, destroy, status and check; use `taloscluster plugin <name>` to run one alone.
- Add `sync` and `apply` as aliases for `converge`.
- Let `init` add missing configuration sections for installed plugins.
- Follow `argocd.monitoring.enabled` and use the CSI provisioner for ArgoCD NFS storage.

### Fixed

- Support Neutron deployments that reject tags in POST, and fail safely on conflicting resource names.
- Include the Rancher cluster ID in ArgoCD cluster values and render the MetalLB pool and ingress IPs.
- Detect modified as well as missing resources in the ArgoCD check.
- Confirm before scale-down and destroy delete nodes or plugin resources.
- Fail converge when both final Talos and Kubernetes health checks fail, and validate cluster names, versions, networks and pools up front.

### Added

- Converge Talos Kubernetes clusters on OpenStack from a declarative `cluster.yaml`, discovering resources by tag with no state file.
- Commands: `init`, `plan`, `converge`, `status`, `check`, `dashboard`, `env`, `image download`, `image remove`, `destroy`, with `-o yaml` on `status` and `check`.
- `check` compares pinned versions against upstream and the running nodes and exits 1 on drift or a leftover cordon.
- Per-pool Talos extensions, freeform machine-config patches, and `tags:` applied as node labels.
- One boot image per Talos version built via factory.talos.dev, Tailscale-based node reachability, and security-group allowlists for the kube and talos APIs.
- Optional `argocd` and `rancher` plugins, installed as extras and inert until configured; a failing plugin is reported and the command exits 1.
