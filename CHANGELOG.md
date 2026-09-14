# Changelog

All notable changes to taloscluster are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Recover a missing kubeconfig from the restored Talos identity before deciding an existing cluster is fresh, so a lost management machine's cluster is reconciled (scale-down, config apply, upgrade) instead of being re-bootstrapped; a scale-up in the same run still boots new nodes at the upgraded version.
- Recover the kubeconfig through the control plane's real address (not the MagicDNS name) on clusters without Tailscale, matching the bootstrap path; `plan` reports a recovered cluster as up and completes, with scale-down and upgrade halting their kube-api reads until the worked-on kubeconfig exists.
- Settle control-plane config applies quorum-safely: `apply_config` reports whether talos restarted the node (`--mode=auto`), so a no-op/live apply skips the settle wait, an unresolved grace-window expiry now refuses to advance instead of warn-and-continue, and a restarted control plane must pass `talosctl health` before the next one is touched.
- Require Talos/etcd health between control-plane removals during scale-down: the kube-api fallback is refused (a responding VIP served by the survivors is no longer trusted), so a surviving control plane is never removed past a member that never left etcd.
- Refuse to delete an addressless control plane during scale-down unless discovery positively confirms its member left the member list — `NotReady` alone no longer bypasses the reset-failure protection.
- Health-check a resumed control-plane rollout before upgrading another node: a node already at the target version/schematic is no longer skipped without re-establishing the `talosctl health` barrier, so an interrupted run whose etcd never recovered aborts instead of advancing past it.
- Redact the bodies of multiline credentials (`password: |`, `token: |`) and of `machine.files`/inline-manifest content in `plan`'s diff even when the change's hunk is truncated without the `content:` header: indented body fragments are suppressed whether they arrive as a `+`/`-` changed line or a ` ` context line, a secret-shaped value (base64/PEM/token or a long unbroken alphanumeric run) is hidden for array entries and for mapping entries at a body depth (shallow `crt:`/`ca:` cert fields stay public), and a password-alias key (`passwd:`) or a secret-named env fragment (`SECRET_KEY=`) is redacted by name no matter its value.
- Suppress a single-`=` padded base64 tail line in `plan`'s diff (a short blob fragment that reads as a public `KEY=` env entry): it is redacted when its `NAME` is blob-shaped, while a genuinely public empty `NAME=` env entry is still shown.
- Fix docs and docstring drift from the 2026-09-07 review: `sdn.exit_nodes` defaults to every cluster node (offline included), the managed-SDN example uses a placeholder instead of a real host, the `kubeapi_vip` move is no longer claimed to happen without a restart, the reachability-timeout message points at the troubleshooting guide, and module docstrings drop the removed terraform/shell/`yq` tooling and OpenStack-only framing.
- Measure the hostname-length check against a pool's real widest ordinal, so a pool of 100+ nodes (three-digit index) is rejected instead of slipping through one character too long.
- Scaffold the Proxmox `kubeapi_vip` at `192.168.0.2`, outside the managed-SDN static layout, so following the inline `sdn: {}` hint no longer collides with the reserved controlplane block.
- Create `talossecrets.yaml` at mode 0600 at creation instead of write-then-chmod, so a fresh secrets file is never briefly world-readable (and a pre-existing broader one is still tightened).
- Contain a `Die` a plugin raises during a hook, so one plugin's fatal abort no longer unwinds the whole run; it is reported like any other plugin failure.
- Detect Tailscale addresses across the whole `100.64.0.0/10`, not just the `100.64.` prefix, so members on `100.65`–`100.127` are still picked as their stable unique address.
- Report the firewall a new Proxmox VM would get during `plan` (policy + rules), so a dry-run shows the same firewall outcome the converge would apply.
- Warn when two plugin entry points share a name instead of silently collapsing to one, and keep the first.
- Add an OpenStack setup guide parallel to the Proxmox one: tenant setup, application credentials, required services and quotas, the `RegionOne` default, external-network selection, availability zones and flavors, and management access.
- Add a tested backup and recovery guide distinguishing provider credentials, Talos identity (`talossecrets.yaml`), derived client configs, etcd snapshots, and application data, including recovery from a lost management machine and an interrupted bootstrap.
- Add a maintenance walkthrough covering replicas, PodDisruptionBudgets, spare worker capacity, scheduling restrictions and storage constraints, with a blocked drain and its resolution; two eligible workers stay a recommendation, not a requirement.
- Add a load-balancer and ingress guide with complete OpenStack and Proxmox examples, showing which addresses core allocates or configures and which MetalLB `IPAddressPool` / `L2Advertisement` and ingress `LoadBalancer` / `Ingress` resources the operator or GitOps setup must supply.
- Document both node management access paths end to end and pin them to the endpoint-selection code with tests: an already-connected Tailscale management machine (the first control plane reached by its MagicDNS name) and direct access to real node addresses without Tailscale (the provider-reported address). Correct the staging notes so the `talosconfig` is described as written before bootstrap, the management endpoint is attributed to `converge`'s `talosctl:` line rather than `status`, and the omit-the-section step is stated once.
- Expand troubleshooting with expected diagnostics and recovery steps for missing Talos secrets (`talossecrets.yaml`), a drain that fails during scale-down, an incomplete version `check`, plugin failures, and an upgrade interrupted mid-rollout, pinning the documented messages and recovery to the code with tests.

- Require the documented YAML types for the ArgoCD options before rendering: `sync`, `automated`, and each per-app `enabled` must be real booleans (a quoted `"false"` is now refused instead of treated as truthy and rendering automated sync with pruning), `admins`/`users` must be lists of email addresses, repository URLs non-empty strings, and the apply-target and credential values plain strings.
- Use the configured OpenStack region (`openstack.region`, default `RegionOne`) in the session, the reported status and `print_environment` output, and the region ArgoCD emits into the cluster-apps values, instead of a hardcoded `RegionOne`.

- Fix Rancher API error messages: a string `message` in the error body is preserved whole instead of joined character by character, and list-form messages and `errors` entries are joined with `; `.
- Refuse a null, non-string, empty, or still-scaffolded `CHANGE-ME` value for the provider credentials and the Tailscale auth key at secrets load time, instead of letting them fail later as an opaque 401.
- Re-verify the managed SDN bridge on every converge (not just after an apply) and wait up to a minute for it to appear, so a missing bridge on a node is reported before a VM is ever placed there.
- Remember a Proxmox disk grow until the node reboots, so a converge without `--reboot` that grows a disk is followed by one with `--reboot` that still finds and restarts the node (the grow is tagged on the VM and cleared on its Proxmox reboot).
- Accept a Proxmox task that finishes with `WARNINGS: N` as success, so converge and destroy no longer abort after a mutation that already succeeded; only a genuine non-`OK` failure is raised.
- Normalize a missing `v` prefix on `talos.version` to the canonical `vMAJOR.MINOR.PATCH` form, so an unprefixed value is no longer compared verbatim against talosctl output and used in factory URLs (which kept every node judged outdated forever).
- Normalize OpenStack security-group rules correctly: a rule with an unrelated `remote_group_id` no longer collides with the open-to-all rule on the same port (so the allowlist is enforced and the foreign rule is removed), and a host CIDR of `0.0.0.0/0` is treated as open instead of being re-created as a duplicate on every run.
- Refuse unknown top-level `cluster.yaml` / `secrets.yaml` keys unless an installed plugin owns the section, so a misspelled or unsupported key is caught instead of silently ignored; plugins declare their top-level sections with `CONFIG_SECTIONS`.
- Validate each ArgoCD per-app section: an unknown per-app key is refused, a `version` override on an app that does not forward it (`ingress`, `nfs`, `monitoring`) is refused instead of silently ignored, and a forwarded version must be a non-empty string.

- Surface unsupported edits that were silently ignored: an OpenStack `flavor`, `disk` or `availability_zone` change on an existing server and a Proxmox placement (`node`) or `storage` change are refused in the validate phase with recreation guidance and reported by `plan`, and an existing OpenStack subnet's DNS is updated in place instead of being ignored. Proxmox `network.dns` on a DHCP-backed (`bridge`/`vnet`) network, where DNS comes from the DHCP server, is warned about instead of implying it is applied.

- Report deferred plugin work during `plan` before the first bootstrap: a configured plugin that needs the cluster's own kubeconfig or an allocated endpoint (such as argocd building its cluster Secret) is reported as deferred and no longer fails the plan with a missing-kubeconfig error.

- Define consistent plugin deletion and failure behavior: a direct `taloscluster plugin NAME destroy` now asks for the cluster name to confirm before it runs (or `--yes` to skip), matching the top-level `destroy`; plugin cleanup failures still leave the infrastructure torn down with a nonzero exit, and plugin `status` errors stay warning-only. Document a `--dry-run` listing what a plugin `destroy` would delete and cover the `--yes`/`--dry-run` skip paths with tests.

- Validate configured plugin configuration before any cluster mutation: a plugin's `validate` hook runs in converge's validate phase, so the argocd plugin refuses repository URLs that are not a pair, git credentials without a Git URL, malformed sections, and unsupported options with the cluster still untouched; `plan` reports the same.
- Pass the Proxmox `ingress_pool` through the provider context to ArgoCD, so the plugin renders the MetalLB address pool (ranges verbatim, single OpenStack VIPs as `/32`) for both providers instead of leaving Proxmox load-balancer addresses blank.
- Split ArgoCD sync semantics: `argocd.sync` now only sets the chart's Helm `sync` value, while a new `argocd.automated` controls automated sync, pruning, and self-healing on the two parent Applications (previously always on).
- `plan` no longer leaks registry `password:`, `machine.files`/`cluster.inlineManifests` contents, or `PASSWORD=`-style environment entries in the machine-config diff; redaction now covers those alongside `key`/`secret`/`token`.
- Do not treat a single failed kube-api probe as a fresh cluster: converge retries the probe and warns loudly when an existing cluster is unreachable, instead of recreating its nodes or attempting a bootstrap.
- Settle machine-config applies on control planes by default, waiting each node out of the cluster and back in before the next, so a reboot-requiring patch no longer restarts every control plane at once.
- Align the AppProject `user` role's policy subject with its role name (`read-only` → `user`) so a configured read-only member actually gets `get` access on the project's applications.
- Abort a control-plane scale-down instead of deleting the VM when the graceful `talosctl reset` fails or times out, so a half-reset etcd member is never left behind; also health-check between successive control-plane removals so quorum is never lost.
- Do not accept kube-api readiness as healthy after a control-plane upgrade or reboot: an upgraded control plane must pass `talosctl health` (and so rejoin etcd) before the rollout advances.
- Refuse to resume pending SDN `deleted` or `changed` state on the cluster's own zone, VNet, subnet, or the shared controller; only `new` is a leftover of an interrupted create, so a staged deletion can no longer be committed by converge under running VMs.
- Guard Rancher `destroy` with the same downstream-id check as `converge`, so it no longer deletes whichever Rancher cluster shares the configured name.
- Resolve Rancher members on an exact principal id match, so a short or misspelled netid no longer grants `cluster-owner` to whoever a prefix search returns first; reject a netid listed under both `rancher.admins` and `rancher.users`.
- Build machine configs for nodes scaled up in the same run as a Kubernetes upgrade at the upgraded version, instead of the pre-upgrade running version they would otherwise boot.
- Recommend at least two workers and spare capacity for node maintenance.
- Shorten the README and organize the documentation around installation, quickstart, usage, commands, configuration, plugins, and troubleshooting.
- Clarify that users must connect the management machine to the cluster's tailnet themselves.
- Align documentation with provider behavior, plugin settings, firewall rules, command limitations, and installation requirements.
- Track audit follow-ups and remaining documentation work in `todo.md`.
- Note that the README's editable tool install needs `uv sync --extra dev` before `uv run pytest` and `uv run mkdocs serve`, and drop the duplicated command.
- Emit no `--login-server` argument when `tailscale.login_server` is unset, so omitting it selects the public Tailscale control plane instead of `--login-server=None`.
- Detect extension-only edits by comparing a node's running schematic (the factory's `schematic` extension in `talosctl get extensions`) instead of the installer reference in the just-applied machine config, so adding or removing an extension reliably reinstalls the node and an extension-only upgrade waits for the new schematic to appear; after a bootstrap or scale-up, any node that came up on the shared base image without its configured extensions is reinstalled too.
- Refuse to generate a fresh `talossecrets.yaml` when infrastructure already exists; converge checks for existing machines before minting a new cluster identity and demands restoration from backup instead.
- Treat an incomplete `check` (unreachable upstream releases, an unknown node version, or a cluster that should exist but answered nothing) as not up to date: the report gains `incomplete` and `incomplete_reasons`, and it exits nonzero instead of silently passing an unverified cluster.
- Refuse unsupported Proxmox changes (disk shrink, NIC bridge/VLAN move, external-NIC detach) in a validation phase at the very start of converge, ahead of the image, network and Talos phases, so a rejected `cluster.yaml` edit no longer leaves a half-applied cluster.
- Deliver the OpenStack Cinder cloud.conf to the downstream cluster as a Secret instead of embedding the provider credential in ArgoCD Application values.
- Activate the ArgoCD plugin only on a kubectl apply target (`kubeconfig` or `context`); a `url`/`token` pair alone no longer shows the plugin as configured and then fails every hook.

## [0.7.0] - 2026-09-06

### Added

- Reconcile Proxmox VM sizing on every converge: `cores` and `memory` are updated in place and `disk` is grown online; `plan` reports each change. Nodes whose new cores or memory waits on a restart are listed on every run until they restart, and `converge --reboot` restarts them one at a time (control planes first, health-checked in between) through a Proxmox reboot, since a guest-initiated reboot does not apply pending sizing. Shrinking a disk, moving a NIC to another bridge/VLAN/VNet, or adding/removing the `external:` section is refused before any mutation with recreation guidance instead of being silently ignored.
- Point every talosctl call at controlplane-01's real address when tailscale is off (SDN static address, guest-agent inventory, or the endpoint recorded in the talosconfig) instead of the kube-api VIP; converge keeps the talosconfig endpoint current.
- Move the kube-api VIP of a running cluster when `kubeapi_vip` changes: control planes are re-applied one at a time (endpoint, certificate SANs, Layer 2 VIP), the kubeconfig is regenerated from a control plane, converge waits for the API on the new address, then the workers follow; `plan` reports the move and the per-node diff.
- Reject a `kubeapi_vip` inside `ingress_pool`, where MetalLB could hand the API address to a service.
- Redact keys, secrets and tokens from the machine-config diff `plan` prints.
- Exclude both the current and the previous kube-api VIP when picking node addresses from Talos discovery, so control-plane-01 is never addressed by a VIP during a move.
- Show the machine-config diff under `plan` using `talosctl apply-config --dry-run`, so plan output tells you what converge would push to each node.
- Document Proxmox API token permissions, managed EVPN SDN with its host prerequisites, post-creation `cluster.yaml` changes, and the link-local anchor / return-path design in the README.
- Add `pytest-timeout` (30s per test) and `pytest-cov` to the dev extra, with a `tests` workflow running the suite with coverage on pushes and pull requests.
- Add a `docs/` configuration reference: an index of every `cluster.yaml` and `secrets.yaml` key with one page per section, including the rancher and argocd plugins.
- Add an MkDocs Material site for `docs/` (`uv run mkdocs serve`), published to GitHub Pages by a `docs` workflow on pushes to main.

### Changed

- Replace site-specific addresses and hostnames in the README, tests and plugin docs with documentation placeholders; the argocd plugin's infrastructure chart repository and NFS servers are now set through `argocd.infra.url` and `argocd.nfs.servers` in `cluster.yaml` instead of being hardcoded.
- Require `talos.version` v1.13.0 or newer; older versions are refused at configuration load.
- Generate the DHCP link and API VIP as `LinkConfig`, `DHCPv4Config`, and `Layer2VIPConfig` documents on OpenStack and on Proxmox without an external NIC, and nameservers on managed SDN as a `ResolverConfig` document, replacing the deprecated `machine.network.interfaces` and `machine.network.nameservers` fields.
- Render the `security:` allowlists as the Talos ingress firewall on every node as well: default action block, all traffic from `network.cidr`, the open-by-default ports, each rule's port from its hosts, DHCP and tailscale. Existing clusters get it on their next converge.
- Upgrade Kubernetes through `talosctl upgrade-k8s` one minor at a time: machine configs for a running cluster are generated with the Kubernetes version it runs, so applying them no longer swaps the kubelet and control-plane images straight to the target and skips the intermediate minors. A `kubernetes.version` older than the running cluster is refused.
- Retry `talosctl bootstrap` while Talos answers "bootstrap is not available yet" (apid is up before etcd is ready), for up to five minutes, instead of failing the first converge.
- Redact `NAME=value` environment entries such as the Tailscale auth key from the machine-config diff `plan` prints; only `key: value` mappings were redacted before.

## [0.6.0] - 2026-09-03

### Added

- Add opt-in managed Proxmox SDN via `proxmox.network.cluster.sdn`: reconcile an EVPN zone, VNet, and SNAT subnet with derived defaults, refuse unrelated pending SDN changes, and verify the bridge on every compute node after apply. The zone/VNet id is `sdn.name` (default: the cluster name, 2-8 chars); a same-named foreign zone or VNet refuses before anything is staged.
- Create the default EVPN controller (`evpnctl`) with peers from the cluster when missing; an existing controller is used untouched and never deleted.
- Assign deterministic static node addresses from `network.cidr` in SDN mode (an EVPN overlay has no DHCP), refuse to silently renumber a running node, and reject a `kubeapi_vip` inside the static layout (gateway, controlplane range, or a worker pool block) so scaling a pool cannot assign a node the VIP.
- Require `SDN.Allocate` and `SDN.Audit` on `/sdn` in the permission preflight when SDN is managed.
- Work without tailscale: when cluster.yaml has no `tailscale:` section, bootstrap resolves cp-01's real address from the provider (SDN static or guest agent), and every other talosctl path (scale-down, machine-config apply, upgrade, status, dashboard, the generated talosconfig endpoint) uses the API VIP instead of the MagicDNS name that would never resolve.
- Skip the shared kube-api VIP when picking a node's address from Talos discovery, so cp-01 is targeted by its own address rather than whichever node currently owns the VIP.
- Omit the tailscale extension from the installed system (`machine.install.image` schematic) when cluster.yaml has no `tailscale:` section; the shared boot ISO still bakes it. Takes effect at install or the next Talos upgrade; list `siderolabs/tailscale` under `talos.extensions` to force it.

### Changed

- Destroy also removes the owned SDN subnet, VNet, and zone after the VMs and pool, keeping anything foreign.

## [0.5.0] - 2026-09-01

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
- Collapse per-node Proxmox firewall reconcile output into a single summary line instead of one line per rule.

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
- `argocd` and `rancher` are plugins of `taloscluster` instead of standalone tools; converge, plan, destroy, status and check run them automatically.
- The standalone `argocd` and `rancher` commands are gone; use `taloscluster plugin <name>` to run one on its own.

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
- The argocd plugin rendered an empty metallb address pool and empty ingress IPs: it read them from the `clusterctl` binary, gone since the rename, and ignored the failure.

### Added

- Converge Talos Kubernetes clusters on OpenStack from a declarative `cluster.yaml`; no state file, resources are discovered via tags.
- Commands: `init`, `plan`, `converge`, `status`, `check`, `dashboard`, `env`, `image download` / `image remove`, `destroy`.
- `check` compares pinned versions against upstream and the running nodes; exits 1 on an update, drift, or a leftover cordon.
- Converge uncordons nodes left `SchedulingDisabled` by an interrupted upgrade.
- Per-pool Talos extensions and freeform machine-config patches.
- One boot image per talos version, built via factory.talos.dev.
- Tailscale-based node reachability.
- Security-group allowlists for the kube and talos APIs.
- `tags:` in `cluster.yaml` applied as node labels; pool tags win over cluster-wide.
- Every node is labeled `ncsa/project` with its OpenStack project.
- `status` also prints the OpenStack endpoint/region/project and the kube-api / ingress floating ips.
- `-o yaml` on `status` and `check` for machine-readable output.
- The kube-api and ingress ports join the cluster security group.
- Optional plugins, installed as `taloscluster[argocd]`, `taloscluster[rancher]` or `taloscluster[all]`, and inert until configured in cluster.yaml/secrets.yaml.
- `taloscluster plugin list` and `taloscluster plugin NAME [ACTION]`.
- `rancher` plugin: import the cluster, install the agent, reconcile members.
- `argocd` plugin: apply the cluster secret, app project and applications.
- A plugin that fails is reported without stopping the others; the command exits 1.
