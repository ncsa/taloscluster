# TODO

Follow-up work from the documentation and implementation audit on 2026-09-07. These are outstanding tasks, not supported features or committed release dates. Keep behavior changes, regression tests, and the matching documentation together.

## Priority 1: correctness and safety

- [ ] Fix the omitted Tailscale login-server case. Generate no `--login-server` argument when unset instead of `--login-server=None`; verify both public Tailscale and explicit Headscale configuration. See [machine configuration](taloscluster/talos/machineconfig.py) and [Tailscale documentation](docs/configuration/tailscale.md).
- [ ] Make extension-only changes reliably trigger a Talos upgrade. Compare the running extension/image state rather than the installer reference in a machine configuration that converge has just updated; cover extension addition, removal, and OpenStack first-boot behavior. See [converge](taloscluster/converge.py) and [Talos helpers](taloscluster/talos/talosctl.py).
- [ ] Refuse to generate a new `talossecrets.yaml` when the cluster already exists. Converge currently generates missing secrets before discovering existing machines; check existing infrastructure first and require restoration from backup. Verify that first-time creation still works. See [converge](taloscluster/converge.py) and [state handling](taloscluster/state.py).
- [ ] Distinguish an incomplete `check` from a successful complete check. Missing upstream data or unknown node versions can currently produce exit status 0; define report fields and exit behavior for incomplete checks and test unreachable services. See [check behavior](docs/commands.md#check).
- [ ] Move unsupported Proxmox change checks ahead of mutations. Disk shrink and NIC attachment checks currently happen in the compute phase after earlier phases may have changed infrastructure or Talos configuration. Verify a rejected change leaves resources unchanged. See [Proxmox reconciliation](taloscluster/proxmox/backend.py).
- [ ] Replace OpenStack credentials embedded in ArgoCD Application Helm values with an appropriate secret reference or delivery mechanism. Verify generated Applications no longer expose the provider credential and document the required permissions. See [ArgoCD manifest generation](plugins/argocd/taloscluster_argocd/manifests.py).
- [ ] Investigate and correct the ArgoCD read-only role policy. The generated role is named `user`, but its policy subject uses `read-only`; verify effective access for a configured user. See [AppProject generation](plugins/argocd/taloscluster_argocd/manifests.py).

## Priority 2: consistent behavior and supported configuration

- [ ] Resolve ArgoCD sync semantics. Decide whether `argocd.sync` controls only chart values or also the two parent Applications; expose any separate control clearly and test the false case. Both parent Applications currently enable automated sync, pruning, and self-healing. See [ArgoCD configuration](docs/configuration/argocd.md#argocdsync).
- [ ] Align ArgoCD activation with supported connection modes. Either implement URL/token operations or reject that mode clearly instead of showing the plugin as configured and then failing every hook.
- [ ] Pass Proxmox ingress ranges through the provider context to ArgoCD. Render MetalLB address ranges correctly rather than relying on the single OpenStack VIP format; verify generated values for both providers.
- [ ] Validate plugin configuration before core mutations. Catch missing paired repository URLs, credentials without a Git URL, malformed settings, and unsupported options before cluster changes begin.
- [ ] Define consistent plugin failure and deletion behavior. Review direct `plugin NAME destroy` confirmation, infrastructure teardown after plugin cleanup failures, and warning-only load/init/status errors; document and test the chosen behavior.
- [ ] Make planning usable before the first bootstrap when plugins are configured. Report deferred plugin work clearly when the downstream kubeconfig or allocated endpoints do not exist yet.
- [ ] Detect unsupported edits that are currently ignored, or add explicit support. Cover OpenStack flavor/disk/availability-zone changes and existing-subnet DNS updates, plus Proxmox placement/storage changes and DNS on DHCP-backed networks. Surface each supported or unsupported action in `plan`.
- [ ] Review core and plugin key validation. Catch misspelled or unsupported keys while retaining valid installed-plugin sections; validate version overrides instead of silently ignoring unsupported per-app keys.

## Documentation and operational guides

- [ ] Add an OpenStack setup guide parallel to [Proxmox setup](docs/providers/proxmox.md): application credentials, required services and quotas, `RegionOne`, external-network selection, flavors, and management access.
- [ ] Add a tested backup and recovery guide distinguishing provider credentials, Talos identity, client configuration, etcd snapshots, and application data. Include recovery from a lost management machine and interrupted bootstrap.
- [ ] Add a maintenance walkthrough with replicas, PodDisruptionBudgets, spare worker capacity, scheduling restrictions, and storage constraints. Show a blocked drain and its resolution; retain the recommendation of at least two eligible workers without making it a hard configuration requirement.
- [ ] Add complete load-balancer and ingress examples for OpenStack and Proxmox. Show which addresses core taloscluster allocates or configures and which MetalLB resources the operator or GitOps setup must supply.
- [ ] Test and document both access paths end to end: an already-connected Tailscale management machine and direct access to real node addresses without Tailscale.
- [ ] Add troubleshooting for missing Talos secrets, failed drains, incomplete version checks, plugin failures, and interrupted upgrades, with expected diagnostics and recovery steps.

## Verification and publication

- [ ] Turn the useful documentation audit checks into maintained checks: load complete YAML examples with matching secrets, validate internal links and anchors, and compare command-reference coverage with the CLI.
- [ ] Exercise the documented quickstart, upgrade, drain, and teardown workflows on disposable OpenStack and Proxmox clusters. Local tests and documentation builds do not verify live provider behavior.
- [ ] Publish the reorganized documentation and verify README links on the deployed site. Preserve existing `concepts/` URLs or add redirects if files move later.
