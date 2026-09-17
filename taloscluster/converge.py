"""The converger.

Enforces a strict phase order, the crux being that existing nodes are upgraded
to the target versions BEFORE new ones are added, so a new node never joins
newer than the rest:

  validate -> image -> secrets -> network/SG -> discover -> scale-down ->
  upgrade -> compute -> bootstrap -> kubeconfig -> health -> plugins

`validate` refuses provider changes that cannot be reconciled in place (an
OpenStack flavor, disk or availability-zone change, a Proxmox placement,
storage, disk-shrink or NIC attachment move) before any phase mutates, so a
rejected change never leaves a half-applied cluster.

Plugins run last because they need a reachable cluster and the kubeconfig this
run just wrote; on destroy they run first, for the same reason inverted.

State is not held in a file (except the Talos secrets); every phase re-derives
what exists through the selected infrastructure backend, so the whole thing is
safe to re-run.
"""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

from . import plugins, versions
from .config import (
    Config,
    Machine,
    Secrets,
    load_config,
    load_secrets,
    validate_warnings,
)
from .context import Context
from .errors import ReconcileError, StateError, preflight_tools
from .infrastructure import (
    InfrastructureBackend,
    InfrastructureInventory,
    NetworkResult,
    backend_for,
    resolve_node_address,
)
from .k8s import kubectl
from .output import action, dry_run, info, log, warn
from .output import report as print_report
from .state import State
from .talos import factory, machineconfig, talosctl


def converge(root: Path, assume_yes: bool = False, reboot: bool = False) -> int:
    """Make the cluster match cluster.yaml. Returns a non-zero exit code when
    an installed plugin failed, or when an existing cluster remains unreachable
    -- an incomplete converge that must not read as a clean no-op. A plugin
    failure happens only once the cluster itself is already built, so a
    downstream registration failure must not look like a converge that did not
    happen; an unreachable existing cluster is the reverse -- nothing was
    reconciled, so it must not look like one that did."""
    cfg = load_config(root)
    secrets = load_secrets(root)

    log("preflight")
    preflight_tools()
    for w in validate_warnings(cfg):
        warn(w)
    if secrets.tailscale_auth_key is None:
        info("no tailscale key -> tailscale extension will idle (node still boots)")

    state = State(root)
    talosconfig_path = root / "talosconfig"
    kubeconfig_path = root / "kubeconfig"
    machines = cfg.machines

    backend = backend_for(cfg, secrets)

    # installer image ref and schematic id per extension set (the schematic
    # drives extension removal; converge compares it against the RUNNING node to
    # detect extension-only changes, see _upgrade)
    installer_platform = backend.installer_platform
    installer_schematics = {s: factory.schematic_id(s) for s in cfg.extension_sets()}
    installer_images = {
        s: factory.installer_image(sid, cfg.talos_version, platform=installer_platform)
        for s, sid in installer_schematics.items()
    }

    # ---- 1. INVENTORY + SUPPORTED-CHANGE PREFLIGHT ------------------------
    # Load what exists before anything mutates so a provider change that cannot
    # be reconciled in place (an OpenStack flavor, disk or availability-zone
    # change, a Proxmox disk shrink, placement or storage change, or a NIC
    # attachment move) is refused while the cluster is still untouched -- not
    # after the image, network or Talos phases already ran. The state and
    # network phases reuse this load.
    log("validate")
    inv = backend.load_inventory()
    backend.validate_machines(machines, inv)
    # Validate configured plugin sections ahead of any cluster change, so a
    # malformed or contradictory plugin configuration stops the run here -- not
    # as a late plugin failure once the image, network and machines mutated.
    # The plugin activation/validate hooks re-read cluster.yaml and secrets.yaml
    # locally from disk -- no provider traffic, so nothing needs fetching here.
    plugins.validate(Context(root=root, cfg=cfg))

    # ---- 2. IMAGE --------------------------------------------------------
    log("image")
    boot_image = backend.ensure_boot_artifact()

    # ---- 3. STATE (talos machine secrets) --------------------------------
    # Loaded above: machines already exist, so we must refuse to mint a new
    # identity -- the secrets are the cluster's irreplaceable CA + tokens and
    # must be restored from backup instead. The network phase reuses this too.
    log("secrets")
    secrets_path = state.secrets_path
    if not state.secrets_exist():
        if inv.machines:
            raise StateError(
                f"{secrets_path} is missing but {len(inv.machines)} machine(s) "
                f"exist ({', '.join(sorted(inv.machines))}). This file is the "
                "cluster's irreplaceable identity (CA + tokens) and cannot be "
                "regenerated for an existing cluster -- restore it from backup."
            )
        action("generate talos machine secrets (first run)")
        if not dry_run():
            state.write_secrets(talosctl.gen_secrets(cfg.talos_version))
    else:
        info(f"machine secrets: {secrets_path} (CRITICAL -- back this up)")

    # ---- 3. NETWORK + SECURITY -------------------------------------------
    log("network + security group")
    refs = backend.reconcile_network(machines, inv)
    info(
        "kubeapi advertised "
        f"{refs.kubernetes.advertised_address or '(pending)'} "
        f"vip {refs.kubernetes.vip or '(pending)'}"
    )

    moving_from = _endpoint_move(kubeconfig_path, cfg.name, refs.kubernetes.advertised_address)

    # write the client talosconfig now that the endpoint (fip) is known. cp-01's
    # tailscale name goes in as the context endpoint so a hand-typed `talosctl`
    # needs no -e; -n stays mandatory. taloscluster itself still passes both.
    if state.secrets_exist() and refs.kubernetes.advertised_address and not dry_run():
        _write_talosconfig(
            talosconfig_path,
            cfg,
            refs,
            secrets_path,
            _talos_endpoint(cfg, refs, inv, talosconfig_path, required=False),
        )

    # ---- 4. DISCOVER: is the cluster reachable? --------------------------
    # The only robust "needs bootstrap" signal is that the kube-api does not
    # answer. We don't trust a persisted marker (survives destroy) or "servers
    # exist" (servers can exist un-bootstrapped: a create that didn't reach
    # bootstrap drops machines into the inventory before the cluster ever
    # bootstrapped). bootstrap itself is idempotent -- on an already-bootstrapped
    # cluster it reports "already bootstrapped" and we treat that as success --
    # so a never-bootstrapped cluster is safe to bootstrap even when its API
    # does not answer. The signal that a cluster WAS bootstrapped is the
    # kubeconfig a prior converge wrote only after bootstrap completed: the
    # probe is retried so one transient failure is never read as fresh, and when
    # that kubeconfig exists yet the API still does not answer, converge warns
    # loudly and refuses to recreate nodes or re-bootstrap (an interrupted first
    # run -- machines but no kubeconfig -- still bootstraps). A recovered
    # management machine restored the identity but not the derived kubeconfig,
    # so a missing kubeconfig with live machines triggers recovery from the
    # restored identity before the fresh/up decision is made. The endpoint and
    # dial target are the same real control plane (`_talos_endpoint` resolves
    # cp-01's tailnet name when tailscale is on, else its real address -- never
    # the kube-api VIP -- since talosctl uses `-n` as the apid dial target).
    cp1_endpoint = _talos_endpoint(cfg, refs, inv, talosconfig_path, required=False)
    up = _kube_up(
        kubeconfig_path,
        inv,
        recover=state.secrets_exist() and bool(inv.machines),
        talosconfig=talosconfig_path,
        endpoint=cp1_endpoint,
        node=cp1_endpoint,
    )
    bootstrapped_before = kubeconfig_path.is_file() and kubeconfig_path.stat().st_size > 0
    existing_but_down = not up and bool(inv.machines) and bootstrapped_before
    if existing_but_down:
        info("cluster not up: existing but unreachable -- not bootstrapping")
    else:
        info(f"cluster {'UP' if up else 'not up (will bootstrap if needed)'}")

    # ---- machine configs (need the fip/vip from the network phase) -------
    default_tags = backend.default_node_tags()

    configs: dict[str, str] = {}
    config_kubernetes_version: str | None = None
    if state.secrets_exist() and refs.kubernetes.advertised_address:
        contributions = {
            host: backend.talos_contribution(m, refs.kubernetes) for host, m in machines.items()
        }
        config_kubernetes_version = _config_kubernetes_version(cfg, kubeconfig_path, up)
        configs = machineconfig.build_configs(
            cfg,
            secrets,
            machines,
            refs.kubernetes,
            secrets_path,
            installer_images,
            contributions,
            default_tags=default_tags,
            kubernetes_version=config_kubernetes_version,
        )

    # ---- 5. SCALE-DOWN ---------------------------------------------------
    if up:
        _scale_down(
            backend,
            cfg,
            machines,
            inv,
            refs,
            talosconfig_path,
            kubeconfig_path,
            assume_yes=assume_yes,
        )

    # ---- 6. MACHINE CONFIG (existing nodes) ------------------------------
    # Before the upgrade phase on purpose: cluster.extraManifests lives in the
    # machine config, and `talosctl upgrade-k8s` refuses to finish until every
    # bootstrap manifest reconciles -- so a manifest fix has to land first.
    if up and configs and not dry_run():
        _apply_existing_configs(
            cfg, machines, inv, refs, configs, talosconfig_path, kubeconfig_path, moving_from
        )
    elif up and configs:
        _apply_configs(cfg, machines, inv, refs, configs, talosconfig_path, kubeconfig_path)

    # ---- 7. UPGRADE (before adding new nodes) ----------------------------
    if up:
        _upgrade(
            cfg,
            machines,
            inv,
            refs,
            installer_images,
            installer_schematics,
            talosconfig_path,
            kubeconfig_path,
        )

    # ---- 7. COMPUTE (create / scale up) ----------------------------------
    log("compute")
    needs_restart: set[str] = set()
    if existing_but_down and not dry_run():
        warn(
            "skipping compute: machines exist but the kube-api is unreachable -- "
            "refusing to recreate nodes for an existing cluster"
        )
    elif configs or dry_run():
        # `configs` baked the running version so `talosctl upgrade-k8s` steps the
        # EXISTING cluster through every minor. A node scaled up in the same run
        # as an upgrade boots at the target version the upgrade phase established.
        # Fresh clusters already baked the target (config_kubernetes_version ==
        # cfg.kubernetes_version), so skip the rebuild -- only scale-ups against a
        # cluster being stepped through minors need regenerating for new nodes.
        if configs and config_kubernetes_version != cfg.kubernetes_version:
            configs.update(
                _new_node_configs(
                    cfg,
                    secrets,
                    machines,
                    inv,
                    refs,
                    secrets_path,
                    installer_images,
                    contributions,
                    default_tags,
                )
            )
        needs_restart = backend.reconcile_machines(machines, inv, boot_image, configs) or set()
    else:
        warn("skipping compute: no machine configs (network fip not ready)")

    # ---- 7b. REBOOT (opt-in) for changes the provider applied but the
    # running machine has not picked up (cores, memory, a grown disk)
    if needs_restart and reboot and up:
        _reboot_nodes(
            backend, cfg, machines, inv, refs, needs_restart, talosconfig_path, kubeconfig_path
        )
    elif needs_restart:
        warn(
            f"{len(needs_restart)} node(s) need a restart to pick up their new sizing: "
            + ", ".join(sorted(needs_restart))
            + " -- rerun with `converge --reboot` to reboot them one at a time"
        )

    # talosctl control operations go through cp-01's tailscale name (this host
    # must be on the tailnet anyway), which is always reachable -- unlike the
    # kube-api floating ip, whose routing from this host isn't guaranteed. The
    # VIP is the talos "node"; the fip stays the kube-api server URL in the
    # kubeconfig. Without tailscale there is no MagicDNS name to resolve, so
    # cp-01's real address is used instead (this host must route to it).
    cp1 = f"{cfg.name}-controlplane-01"
    if not cfg.tailscale_enabled and not dry_run():
        cp1 = _resolve_cp1_address(backend, cfg, refs) or cp1
        # the talosconfig written above may predate cp-01's address (first
        # run) or carry a stale DHCP lease; keep it pointing at the real node
        if state.secrets_exist() and refs.kubernetes.advertised_address:
            _write_talosconfig(talosconfig_path, cfg, refs, secrets_path, cp1)

    # ---- 8. BOOTSTRAP (if the cluster isn't up) --------------------------
    if not up and not existing_but_down and not dry_run():
        log("bootstrap")
        # a freshly created node must boot, start tailscale, and register with
        # headscale before its name resolves -- wait for it (pre-VIP: node=cp1)
        _wait_reachable(talosconfig_path, cp1, cp1)
        # idempotent: on an already-bootstrapped cluster this is a no-op
        talosctl.bootstrap(talosconfig_path, endpoint=cp1, node=cp1)

    # ---- 9. KUBECONFIG ---------------------------------------------------
    if (
        not up
        and not existing_but_down
        and state.secrets_exist()
        and refs.kubernetes.vip
        and not dry_run()
    ):
        log("kubeconfig")
        # wait for cp-01 (already confirmed reachable above during bootstrap);
        # don't use the VIP as the node -- it may have moved to another CP
        # during a reboot and is not reliably announced yet.
        _wait_reachable(talosconfig_path, cp1, cp1)
        talosctl.kubeconfig(talosconfig_path, cp1, cp1, kubeconfig_path)

    # ---- 10. HEALTH + STATUS ---------------------------------------------
    # Health checks are meaningless on a cluster already known unreachable, and
    # would fail or hang (talosctl retries, node_summary returning [] against a
    # dead API), so skip them for an existing-but-unreachable cluster.
    if not dry_run() and not existing_but_down and refs.kubernetes.vip:
        log("health")
        _require_final_health(talosconfig_path, cp1, refs.kubernetes.vip, kubeconfig_path)
        _wait_nodes_ready(kubeconfig_path, machines)
        # OpenStack first-boot and scaled-up nodes are created on the shared
        # base image, so re-check every node's running schematic now that it has
        # joined and reinstall any that came up short of its configured
        # extensions (a no-op on nodes the upgrade phase already converged).
        inv = _reconcile_joined(
            cfg,
            machines,
            backend,
            refs,
            installer_images,
            installer_schematics,
            talosconfig_path,
            kubeconfig_path,
        )
        log("status")
        print(kubectl.get_nodes_wide(kubeconfig_path))
        print(f"kube api:   https://{refs.kubernetes.advertised_address}:6443")
        print(f"ingress ip: {refs.ingress.advertised_address} (reserved)")
        print(f"talosctl:   talosctl --talosconfig {talosconfig_path} -e {cp1} -n {cp1} <cmd>")
        print(f"kubectl:    kubectl --kubeconfig {kubeconfig_path} get nodes")
        backend.finalize_machines(inv)

    # ---- 11. PLUGINS -----------------------------------------------------
    # built from what this run already computed, so no plugin can trigger a
    # second round-trip to OpenStack for facts we are holding right here.
    advertised = refs.kubernetes.advertised_address
    api_url = f"https://{advertised}:6443" if advertised else ""
    provider_status = backend.provider_status()
    ctx = Context.from_converge(
        root,
        cfg,
        kubeapi={"floating_ip": advertised, "vip": refs.kubernetes.vip, "endpoint": api_url},
        ingress={
            "floating_ip": refs.ingress.advertised_address,
            "vip": refs.ingress.vip,
            "metallb": list(refs.metallb),
        },
        infrastructure={"provider": backend.name, **provider_status},
        openstack=provider_status if backend.name == "openstack" else {},
    )
    if existing_but_down and not dry_run():
        # The plugin converge hooks mutate against a live cluster (deploy an
        # ArgoCD App, reconcile Rancher membership), so they must not run
        # against a cluster we cannot reach -- and this converge is incomplete,
        # so it fails rather than reporting a clean exit until the cluster can
        # be reconciled. Plan/dry-run leaves them in place so plan still shows
        # what a successful converge would do.
        warn(
            "cluster is existing but unreachable: deferring plugin converge "
            "hooks and reporting an incomplete converge"
        )
        return 1
    return _run_plugins(ctx, "converge", assume_yes=assume_yes)


def _run_plugins(ctx: Context, hook: str, reverse: bool = False, **kw) -> int:
    """Fan a hook out to every plugin configured for this cluster directory."""
    active = plugins.active(ctx)
    if not active:
        return 0
    if reverse:
        active = list(reversed(active))
    log(f"plugins: {', '.join(p.name for p in active)}")
    return plugins.run(active, hook, ctx, **kw)


# ---------------------------------------------------------------------------
# phases that need cross-resource reasoning
# ---------------------------------------------------------------------------


def _recorded_endpoint(kubeconfig: Path, cluster: str) -> str:
    """The kube-api host the running cluster was last converged to, or "".

    `talosctl kubeconfig` names its cluster entry after the Talos cluster and
    points it at cluster.controlPlane.endpoint, so the kubeconfig converge
    wrote is a provider-neutral record of the endpoint every node, certificate
    and client is bound to.
    """
    try:
        doc = yaml.safe_load(kubeconfig.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ""
    for entry in doc.get("clusters") or []:
        if not isinstance(entry, dict) or entry.get("name") != cluster:
            continue
        server = str((entry.get("cluster") or {}).get("server") or "")
        return urlparse(server).hostname or ""
    return ""


def _reboot_nodes(
    backend: InfrastructureBackend,
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    hosts: set[str],
    talosconfig: Path,
    kubeconfig: Path,
) -> None:
    """Restart `hosts` one at a time, control planes first, and require the
    cluster healthy again before the next -- so a sizing rollout never takes
    two control planes down together.

    The restart goes through the provider, not `talosctl reboot`: a reboot from
    inside the guest keeps the same VM process, so provider-side sizing changes
    (Proxmox pending cores/memory) would never apply. The provider's reboot is
    still an ACPI shutdown that Talos handles gracefully.
    """
    log("reboot")
    endpoint = _talos_endpoint(cfg, refs, inv, talosconfig)
    discovered = talosctl.member_addresses(
        talosconfig, endpoint, exclude_vip=_cluster_vips(cfg, refs, kubeconfig)
    )
    ordered = sorted(
        (h for h in machines if h in hosts),
        key=lambda h: 0 if machines[h].role == "controlplane" else 1,
    )
    for host in ordered:
        address = resolve_node_address(host, discovered, inv, refs)
        if not address:
            warn(f"{host}: no address known, not rebooted")
            continue
        backend.restart_machine(host, inv)  # returns once the provider restarted it
        if dry_run():
            continue
        _wait_reachable(talosconfig, address, address)
        if not _health_or_kube_fallback(
            talosconfig,
            endpoint,
            refs.kubernetes.vip,
            kubeconfig,
            timeout="10m",
            fallback=machines[host].role != "controlplane",
        ):
            raise ReconcileError(f"cluster unhealthy after rebooting {host}; stopping the rollout")
        info(f"{host} rebooted")


def _cluster_vips(cfg: Config, refs: NetworkResult, kubeconfig: Path) -> set[str]:
    """Every kube-api address a node may still carry: the desired VIP and,
    during an endpoint move, the one the cluster was last converged to.
    Discovery lists them among the owner's addresses; none of them is a node."""
    return {refs.kubernetes.vip, _recorded_endpoint(kubeconfig, cfg.name)} - {""}


def _kube_up(
    kubeconfig: Path,
    inv: InfrastructureInventory,
    attempts: int = 3,
    interval_s: int = 10,
    *,
    recover: bool = False,
    talosconfig: Path | None = None,
    endpoint: str | None = None,
    node: str | None = None,
) -> bool:
    """Probe whether the kube-api answers, retrying, so one transient failure
    is never read as a fresh cluster.

    A single ten-second `kubectl get nodes` failure on a live cluster must not
    cascade into scale-down, apply and upgrade being skipped and missing nodes
    being recreated at the target version. So the probe is retried.

    When there is no kubeconfig from an earlier converge the cluster looks
    never-bootstrapped. Two cases share that look and are told apart here:

    * a genuinely fresh or interrupted first run has no kubeconfig because
      bootstrap never completed -- a probe cannot succeed (kubectl
      short-circuits on the missing file), so it is reported straight down for
      the caller to bootstrap it;
    * a recovered management machine has a healthy cluster but a missing
      kubeconfig, since `talosconfig`/`kubeconfig` are derived client files and
      are not restored (see docs/backup.md). When a talos identity and
      infrastructure machines exist (`recover=True`), the kubeconfig is
      regenerated from the restored identity first (`_recover_missing_kubeconfig`),
      so the probe runs against the real cluster instead of assuming fresh; only
      if that recovery produces no kubeconfig (a never-bootstrapped first run, or
      an unreachable node) is the cluster read as never-bootstrapped.

    And when a kubeconfig DOES exist yet the API still does not answer after
    every attempt, the operator is warned loudly that this is an existing but
    unreachable cluster -- not a fresh one -- so `converge` will not recreate
    nodes or re-bootstrap it.
    """
    if not kubeconfig.is_file() or kubeconfig.stat().st_size == 0:
        if recover and talosconfig and endpoint and node:
            recovered = _recover_missing_kubeconfig(talosconfig, endpoint, node, kubeconfig)
            if dry_run() and recovered:
                # plan/dry-run wrote nothing, but recovery prognoses an existing
                # cluster; report it UP (scale-down/apply/upgrade will run) rather
                # than "will bootstrap if needed"
                return True
            if not recovered:
                # no kubeconfig was reproduced: a never-bootstrapped first run
                # (or an unreachable node) -- probe cannot succeed
                return False
        else:
            # never bootstrapped (no kubeconfig); a probe cannot succeed
            return False
    for attempt in range(1, attempts + 1):
        if kubectl.cluster_up(kubeconfig):
            return True
        if attempt < attempts:
            info(f"kube-api did not answer (attempt {attempt}/{attempts}); retrying...")
            time.sleep(interval_s)
    if inv.machines:
        warn(
            f"{len(inv.machines)} machine(s) already exist and a kubeconfig was "
            "written by an earlier converge, but the kube-api does not answer. "
            "This is NOT a fresh cluster: refusing to recreate nodes or "
            "bootstrap. Investigate the cluster before re-running converge."
        )
    return False


def _recover_missing_kubeconfig(
    talosconfig: Path,
    endpoint: str,
    node: str,
    kubeconfig: Path,
    reachable_timeout_s: int = 900,
    interval_s: int = 15,
) -> bool:
    """Regenerate a missing kubeconfig from the restored Talos identity.

    `talosconfig` and `kubeconfig` are derived client configs (see docs/backup.md)
    and are not restored with the cluster directory. On a recovered management
    machine the cluster itself is healthy but the client lost its kubeconfig,
    which otherwise makes ``converge`` read the cluster as never-bootstrapped. Fetch
    a fresh one from the first control plane through the identity the operator
    restored, then let the caller probe it. Returns True only once a non-empty
    kubeconfig was actually written (in dry-run, which writes nothing, True
    reports the recovery prognosis so the caller reads the cluster as up). A
    never-bootstrapped first run fails here (the node runs no api-server to serve
    a kubeconfig) and stays a fresh cluster for the caller to bootstrap.

    The control-plane endpoint is always a real node -- never the kube-api VIP --
    so writing a kubeconfig proves the etcd/control plane behind it is up.
    """
    info(
        f"kubeconfig is missing but {node} and the talos identity exist; "
        "recovering it from the restored identity..."
    )
    if dry_run():
        # plan/dry-run must not write client files; report that a real run would
        # recover the kubeconfig so the cluster is reconciled as existing. Return
        # True so the caller reads the cluster as UP (it would be, once fetched)
        # instead of "will bootstrap if needed".
        action(f"recover kubeconfig from {endpoint}")
        return True
    try:
        _wait_reachable(
            talosconfig, endpoint, node, timeout_s=reachable_timeout_s, interval_s=interval_s
        )
        talosctl.kubeconfig(talosconfig, endpoint, node, kubeconfig)
    except (TimeoutError, subprocess.CalledProcessError, OSError):
        if kubeconfig.is_file():
            # a partial/empty fetch must not read as a usable kubeconfig
            try:
                kubeconfig.unlink()
            except OSError:
                pass
        warn(
            f"could not recover the kubeconfig from {node}: if this is a first "
            "run that never bootstrapped, converge will bootstrap it"
        )
        return False
    return bool(kubeconfig.is_file() and kubeconfig.stat().st_size > 0)


def _endpoint_move(kubeconfig: Path, cluster: str, advertised: str) -> str:
    """The old kube-api endpoint when cluster.yaml moves it, else "".

    A move re-applies every node's cluster endpoint, both certificate SAN sets
    and the control planes' Layer 2 VIP through the machine config (control
    planes first, each settled before the next), then regenerates the
    kubeconfig from a control plane -- see `_finish_endpoint_move`. talosctl
    itself never depends on the VIP, so the move cannot lock us out.
    """
    previous = _recorded_endpoint(kubeconfig, cluster)
    if previous and advertised and previous != advertised:
        action(f"move kube-api endpoint {previous} -> {advertised}")
        return previous
    return ""


def _finish_endpoint_move(
    cfg: Config,
    refs: NetworkResult,
    inv: InfrastructureInventory,
    talosconfig: Path,
    kubeconfig: Path,
    timeout_s: int = 300,
) -> None:
    """After the machine configs carry the new endpoint: new kubeconfig, and
    wait until the kube-api answers on the new address."""
    cp1 = _talos_endpoint(cfg, refs, inv, talosconfig)
    _wait_reachable(talosconfig, cp1, cp1)
    talosctl.kubeconfig(talosconfig, cp1, cp1, kubeconfig)
    new = refs.kubernetes.advertised_address
    info(f"waiting for kube-api on {new} (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    while not kubectl.cluster_up(kubeconfig):
        if time.monotonic() >= deadline:
            raise ReconcileError(
                f"kube-api did not answer on {new} after the endpoint move; the nodes "
                f"carry the new config -- check `talosctl -n <node> get addresses` for {new}"
            )
        time.sleep(10)
    info(f"kube-api answers on {new}")


def _write_talosconfig(
    path: Path, cfg: Config, refs: NetworkResult, secrets_path: Path, client_endpoint: str
) -> None:
    path.write_text(
        talosctl.gen_talosconfig(
            cfg.name,
            refs.kubernetes.advertised_address,
            secrets_path,
            client_endpoint=client_endpoint,
        )
    )
    os.chmod(path, 0o600)


def _talos_endpoint(
    cfg: Config,
    refs: NetworkResult | None = None,
    inv: InfrastructureInventory | None = None,
    talosconfig: Path | None = None,
    *,
    required: bool = True,
) -> str:
    """The endpoint talosctl calls go through: always a real control plane.

    With tailscale that is cp-01's MagicDNS name. Without it, cp-01's real
    address: the managed-SDN static address, else what the provider inventory
    (guest agent) reports, else the endpoint converge recorded in the
    talosconfig on its last run. Never the kube-api VIP -- it belongs to
    whichever node currently owns it, and a cluster.yaml edit could point it
    at an address no node owns yet.
    """
    host = f"{cfg.name}-controlplane-01"
    # getattr: test fixtures and older plugins hand in duck-typed configs
    if getattr(cfg, "tailscale_enabled", True):
        return host
    address = (
        (refs.machine_address(host) if refs is not None else "")
        or (inv.machine_address(host) if inv is not None else "")
        or (_talosconfig_endpoint(talosconfig, cfg.name) if talosconfig is not None else "")
    )
    if address:
        return address
    if required:
        raise ReconcileError(
            f"no address known for {host}: talosctl needs a real control-plane "
            "address without tailscale (the provider has not reported one and no "
            "talosconfig from an earlier converge records it)"
        )
    return host


def _talosconfig_endpoint(talosconfig: Path, cluster: str) -> str:
    """The context endpoint converge last wrote into the talosconfig, or ""."""
    try:
        doc = yaml.safe_load(talosconfig.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ""
    ctx = (doc.get("contexts") or {}).get(doc.get("context") or cluster) or {}
    endpoints = ctx.get("endpoints") or []
    return str(endpoints[0]) if endpoints else ""


def _resolve_cp1_address(backend, cfg, refs, timeout_s: int = 600, interval_s: int = 15) -> str:
    """cp-01's address for clusters without tailscale (no MagicDNS name).

    Managed-SDN static addresses come straight from the network result; a
    bridge-mode DHCP address appears once the freshly booted guest agent
    reports it, so poll the inventory until it does.
    """
    host = f"{cfg.name}-controlplane-01"
    address = refs.machine_address(host)
    if address:
        return address
    info(f"no tailscale: resolving {host} address (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    while True:
        address = backend.load_inventory().machine_address(host)
        if address:
            info(f"{host} -> {address}")
            return address
        if time.monotonic() >= deadline:
            warn(f"could not resolve an address for {host}")
            return ""
        time.sleep(interval_s)


def _wait_reachable(
    talosconfig: Path, endpoint: str, node: str, timeout_s: int = 900, interval_s: int = 15
) -> None:
    """Block until talos apid answers (endpoint -> node), or time out.

    Used both to wait for a fresh node to join the tailnet before bootstrap
    (endpoint=node=cp-01) and to wait for the VIP to be announced after bootstrap
    (endpoint=cp-01, node=VIP). Requires this machine to be on the tailnet.
    """
    info(f"waiting for {endpoint} -> {node} to become reachable (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if talosctl.reachable(talosconfig, endpoint=endpoint, node=node):
            info(f"{endpoint} -> {node} is reachable")
            return
        time.sleep(interval_s)
    raise TimeoutError(
        f"{endpoint} -> {node} did not become reachable within {timeout_s // 60}m. "
        "Is this machine on the tailnet, and is there a stale headscale entry "
        f"for {endpoint}? (see docs/troubleshooting.md"
        "#recreating-a-cluster-reuses-stale-headscale-entries)"
    )


def _wait_nodes_ready(
    kubeconfig: Path, machines: dict[str, Machine], timeout_s: int = 900, interval_s: int = 15
) -> None:
    """Wait for every desired machine to appear as a Ready Kubernetes node.

    Existing machines are already Ready and pass immediately; new machines
    need time to boot, install Talos, and join the cluster. Must complete
    before ``finalize_machines`` detaches credential-bearing cidata ISOs.
    """
    pending = set(machines)
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        ready = {n["name"] for n in kubectl.node_summary(kubeconfig) if n["ready"]}
        pending -= ready
        if pending:
            info(
                f"waiting for {len(pending)} node(s) to become Ready: {', '.join(sorted(pending))}"
            )
            time.sleep(interval_s)
    if pending:
        raise ReconcileError(
            f"nodes did not become Ready within {timeout_s // 60}m: {', '.join(sorted(pending))}"
        )


def _wait_version(
    talosconfig: Path,
    endpoint: str,
    node: str,
    want: str,
    want_schematic: str = "",
    timeout_s: int = 1800,
    interval_s: int = 10,
) -> None:
    """Block until `node` reboots into talos `want` -- and, for an
    extension-only upgrade that keeps the same talos version (`want_schematic`
    given), is on the expected schematic.

    Replaces `talosctl upgrade --wait`, whose watch stream dies with
    ENHANCE_YOUR_CALM/too_many_pings whenever the client is newer than the
    server -- always true mid-upgrade (see talosctl.upgrade). Polling is
    immune to that, and to the node dropping off the network while it reboots.

    The version alone cannot tell an extension-only reboot apart (it is already
    at `want` before the upgrade), so when `want_schematic` is supplied the
    running schematic is the barrier that actually waits out the reboot.

    30m matches talosctl's own upgrade timeout: the node has to pull the
    installer image from factory.talos.dev before it can reboot, and a slow or
    flaky pull is the normal reason this takes a while. A timeout here means
    the upgrade really did not land -- check `talosctl -n <node> dmesg` for
    image-pull errors.
    """
    if dry_run():
        return
    marker = want if not want_schematic else f"{want}/{want_schematic}"
    info(f"waiting for {node} to come back on {marker} (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    seen = ""
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        try:
            seen = talosctl.server_version(talosconfig, endpoint, node)
        except subprocess.CalledProcessError:
            continue  # node is rebooting; apid not answering yet
        if seen != want:
            continue
        if want_schematic:
            try:
                if talosctl.running_schematic(talosconfig, endpoint, node) != want_schematic:
                    continue
            except subprocess.CalledProcessError:
                continue  # node is still down mid-reboot
        info(f"{node} is on {marker}")
        return
    raise TimeoutError(
        f"{node} did not come back on {marker} within {timeout_s // 60}m "
        f"(last seen: {seen or 'unreachable'}). Check `talosctl -n {node} dmesg`."
    )


def _config_kubernetes_version(cfg: Config, kubeconfig: Path, up: bool) -> str:
    """The kubernetes version to bake into the machine configs.

    The generated config carries the kubelet and control-plane images for a
    version, and applying it to a running node swaps them in place -- so
    generating a running cluster's configs with the *target* version upgrades
    kubernetes by config push, skipping every minor in between and leaving
    `talosctl upgrade-k8s` nothing to do. Keep the running version in the
    config; the upgrade phase then steps to the target with upgrade-k8s, which
    rewrites those images itself. Fresh clusters (and an unreachable kube-api)
    use cluster.yaml.
    """
    want = cfg.kubernetes_version
    if not up:
        return want
    cur = _running_kubernetes_version(kubeconfig)
    if cur is None:
        # dry-run plan on a recovered machine: the running version is unknown
        # because no kubeconfig is on disk, but a real run recovers it and steps
        # the minors -- keep the target so the plan completes (see `_upgrade`).
        return want
    if cur == want:
        return want
    if versions.is_older(want, cur):
        raise ReconcileError(
            f"kubernetes.version {want} is older than the running {cur}; "
            "kubernetes downgrades are not supported"
        )
    info(f"machine configs keep kubernetes {cur}; upgrade-k8s moves the cluster to {want}")
    return cur


def _running_kubernetes_version(kubeconfig: Path) -> str | None:
    """The running cluster's kubernetes version, retrying a transient read.

    A reachable cluster answering an empty `kubectl version` is a strong sign of
    a transient probe failure, not that the version is unknown for good --
    reading it as the target version would push target kubelet/control-plane
    images through `_apply_existing_configs` and skip the minor-by-minor
    upgrade. Retry the read and, if it still cannot be established, abort
    before any config mutation. The one exception is a dry-run plan on a
    recovered management machine, which prognoses the cluster up without
    writing a kubeconfig: there is nothing to read, so return None and let
    `_config_kubernetes_version` keep the target instead of aborting the plan
    (mirrors `_upgrade`'s guard).
    """
    if dry_run() and not (kubeconfig.is_file() and kubeconfig.stat().st_size > 0):
        # plan prognosed the recovered cluster as up but wrote no kubeconfig;
        # the running version cannot be read, and a real run recovers it and
        # steps the minors -- nothing for a dry run to mutate
        info("kubernetes version unknown (missing kubeconfig); skipped in plan")
        return None
    for attempt in range(1, 4):
        cur = kubectl.server_version(kubeconfig)
        if cur:
            return cur
        if attempt < 3:
            info(f"kubernetes version read failed (attempt {attempt}/3); retrying...")
            time.sleep(2)
    raise ReconcileError(
        "could not determine the running cluster's kubernetes version while "
        "the kube-api is up; refusing to generate machine configs against an "
        "unknown version. Retry converge or investigate the cluster health."
    )


def _new_node_configs(
    cfg: Config,
    secrets: Secrets,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    secrets_path: Path,
    installer_images,
    contributions,
    default_tags,
) -> dict[str, str]:
    """Machine configs for the nodes that do not exist yet, at the target version.

    `build_configs` above bakes the RUNNING version into the configs so
    `talosctl upgrade-k8s` steps the existing cluster through every minor. A node
    scaled up in the same run as an upgrade has no prior minor to step -- so once
    the upgrade phase has moved the cluster to the target, regenerate the configs
    for the nodes that still do not exist with that target version. Without this
    they would boot one or two minors behind the rest of the cluster.
    """
    fresh = {h for h in machines if h not in inv.machines}
    if not fresh:
        return {}
    return machineconfig.build_configs(
        cfg,
        secrets,
        {h: machines[h] for h in fresh},
        refs.kubernetes,
        secrets_path,
        installer_images,
        contributions,
        default_tags=default_tags,
        kubernetes_version=cfg.kubernetes_version,
    )


def _k8s_upgrade_path(cur: str, want: str) -> list[str]:
    """The versions to step through to get from `cur` to `want`.

    Kubernetes only supports one minor at a time, and talosctl enforces it:

        unsupported upgrade path 1.34->1.36 (from "1.34.1" to "1.36.2")

    So 1.34.1 -> 1.36.2 becomes [1.35.<latest>, 1.36.2]. Intermediate hops use
    the newest patch of that minor (dl.k8s.io), since a stepping stone should
    not be a stale .0. Falls back to <minor>.0 if dl.k8s.io is unreachable --
    still a valid hop, just older.
    """

    def minor_of(v: str) -> tuple[int, int]:
        parts = v.lstrip("v").split(".")
        return int(parts[0]), int(parts[1])

    if not cur:
        # kube-api was unreachable when we asked (it briefly is, right after a
        # machine-config apply). Stepping cannot be computed without a starting
        # point, so hand talosctl the target and let ITS check reject an illegal
        # skip -- better a clear "unsupported upgrade path" than a guess.
        warn("current kubernetes version unknown; attempting a direct upgrade")
        return [want]
    cur_major, cur_minor = minor_of(cur)
    want_major, want_minor = minor_of(want)
    path: list[str] = []
    for m in range(cur_minor + 1, want_minor):  # strictly intermediate hops
        label = f"{cur_major}.{m}"
        try:
            path.append(versions.latest_kubernetes_patch(label))
        except (OSError, requests.RequestException) as e:
            warn(f"could not resolve latest {label} patch ({e}); using {label}.0")
            path.append(f"v{label}.0")
    path.append(want)
    if len(path) > 1:
        info(f"stepping through minors: {' -> '.join(path)}")
    return path


def _uncordon_stale(kubeconfig: Path, host: str) -> None:
    """Lift a cordon `talosctl upgrade` left behind on `host`, if any.

    Talos cordons the node it is upgrading and uncordons it on completion, so
    normally there is nothing to do here -- but the uncordon is skipped whenever
    the upgrade's client-side watch dies (see talosctl.upgrade) or the run is
    interrupted. The node then stays SchedulingDisabled: nothing schedules onto
    it, and every subsequent health check fails on "some nodes are not
    schedulable" while kube-api looks perfectly healthy, which is exactly the
    kind of drift converge exists to remove.

    Only ever lifts a cordon -- taloscluster never cordons a node it keeps, so a
    cordon on a managed node is always stale. A node cordoned BY HAND for
    maintenance therefore gets lifted too; that is the trade converge makes
    everywhere else (cluster.yaml wins over manual state).
    """
    if host not in kubectl.unschedulable(kubeconfig):
        return
    info(f"{host} is cordoned (leftover from the upgrade); uncordoning")
    if not kubectl.uncordon(kubeconfig, host):
        warn(f"could not uncordon {host}; run `kubectl uncordon {host}` by hand")


def _health_or_kube_fallback(
    talosconfig: Path,
    endpoint: str,
    vip: str,
    kubeconfig: Path,
    timeout: str = "5m",
    fallback: bool = True,
) -> bool:
    """talosctl health, falling back to kube-api readiness on failure.

    The talos side of the check targets the endpoint control plane itself; the
    `vip` only names the kube-api URL the server-side check must probe. Retried
    once, because a rolling upgrade reboots the control plane the check runs
    on and resets its connection:

        healthcheck error: ... read tcp ...:50000: connection reset by peer

    That is the reboot, not a sick cluster; by the retry the node is back.

    After a control-plane upgrade or reboot the running node must pass
    `talosctl health` itself -- it is the only signal that the node rejoined
    etcd. The kube-api VIP is still served by the surviving control planes, so
    a responding VIP says nothing about the node being upgraded; accepting it
    would let the rollout advance past a control plane that never came back and
    cost quorum on the next one. So for control planes callers pass
    `fallback=False` and a twice-failed talos health aborts the rollout;
    `fallback=True` (the default) accepts kube-api readiness as a usable
    cluster for non-control planes and the final health check.

    Returns True if the talos signal passes, or -- only when `fallback` is
    True -- the kube-api readiness signal passes.
    """
    for attempt in (1, 2):
        try:
            # k8s_endpoint=vip: the server-side check runs on the node, which cannot
            # reach its own floating ip (no NAT hairpin) -- see talosctl.health()
            talosctl.health(talosconfig, endpoint, endpoint, timeout=timeout, k8s_endpoint=vip)
            return True
        except subprocess.CalledProcessError:
            if attempt == 1:
                info("health check interrupted (VIP failover?); retrying in 30s")
                time.sleep(30)

    # talosctl health probes every node's apid via cluster discovery; it can
    # still fail on a transient tailnet/discovery hiccup even when the cluster
    # is fine. For a control plane we refuse to substitute the kube-api signal
    # (see above); the fallback is only for the rest of the cluster and final
    # health.
    if not fallback:
        warn(
            "talosctl health did not pass twice for a control plane; refusing the kube-api fallback"
        )
        return False
    warn("talosctl health did not pass twice; checking kubernetes readiness instead")
    if kubectl.cluster_up(kubeconfig):
        info("kube-api is up and answering -- cluster is usable")
        return True
    warn("kube-api is not answering either -- investigate the cluster")
    return False


def _require_final_health(talosconfig: Path, endpoint: str, vip: str, kubeconfig: Path) -> None:
    """Fail converge when neither Talos health nor Kubernetes readiness passes."""
    if not _health_or_kube_fallback(talosconfig, endpoint, vip, kubeconfig):
        raise ReconcileError("cluster is unhealthy after converge")


def _scale_down(
    backend: InfrastructureBackend,
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    talosconfig: Path,
    kubeconfig: Path,
    assume_yes: bool = False,
) -> None:
    log("scale down")
    # talosctl endpoint: cp-01's tailscale name (or its real address without
    # tailscale); the node is always a numeric private ip apid can route to.
    endpoint = _talos_endpoint(cfg, refs, inv, talosconfig)
    desired = set(machines)
    if dry_run() and not (kubeconfig.is_file() and kubeconfig.stat().st_size > 0):
        # plan prognosed the recovered cluster as up but wrote no kubeconfig, so
        # there is no live node list to read against (a real run recovers it
        # first); with no nodes known there is nothing to scale down.
        live = []
    else:
        live = kubectl.node_names(kubeconfig)
    # Removals are reconciled from OUR owned provider inventory as well as the
    # live Kubernetes node set: `_scale_down` deletes the kube Node before the
    # provider VM, so a VM deletion that fails strands the VM forever -- a rerun
    # sees no Node and would never notice it. Owned machines that never joined
    # Kubernetes (a worker that failed to register, or a control plane created
    # then dropped from config) are invisible to `kubectl.get nodes` too. Every
    # undesired owned machine must therefore be removed, not just live Nodes.
    live_set = set(live)
    removals = []
    seen: set[str] = set()
    for node in live:
        if node not in desired and node not in seen:
            removals.append(node)
            seen.add(node)
    for node in inv.machines:
        if node not in desired and node not in seen:
            removals.append(node)
            seen.add(node)
    if not removals:
        info("nothing to remove")
        return

    # Validate every removal before prompting so confirmation means the whole
    # displayed operation is safe to start.
    desired_cp = int(cfg.controlplane["count"])
    for node in removals:
        if "-controlplane-" in node and (desired_cp % 2 == 0 or desired_cp < 1):
            raise ReconcileError(
                f"refusing to remove controlplane {node}: desired controlplane "
                f"count {desired_cp} would break etcd quorum"
            )

    warn(f"scale down will remove {len(removals)} node(s): {', '.join(removals)}")
    if not assume_yes and not dry_run():
        resp = input("type the cluster name to confirm: ").strip()
        if resp != cfg.name:
            raise SystemExit("aborted")

    removed = 0
    discovered = (
        talosctl.member_addresses(
            talosconfig, endpoint, exclude_vip=_cluster_vips(cfg, refs, kubeconfig)
        )
        if talosconfig.is_file()
        else {}
    )
    # how many control planes still have to go, so we health-check (etcd quorum
    # must survive) after each one before removing the next
    remaining_cp = sum(1 for n in removals if "-controlplane-" in n)
    for node in removals:
        is_cp = "-controlplane-" in node
        # A removal that came only from the provider inventory has no Kubernetes
        # Node (it never joined, or a prior run deleted the Node before its VM
        # delete failed), so there is nothing to drain or to delete via kubectl.
        has_node = node in live_set
        address = resolve_node_address(node, discovered, inv, refs)
        if address:
            info(f"removing {node} ({address})")
            if has_node:
                try:
                    kubectl.drain(kubeconfig, node)
                except subprocess.CalledProcessError:
                    ready = kubectl.node_ready(kubeconfig, node)
                    if ready is not False:
                        state = "Ready" if ready else "unknown"
                        raise ReconcileError(
                            f"drain of {node} failed and node is {state}; "
                            "aborting to protect a potentially live node"
                        ) from None
                    warn(f"drain of {node} failed (node already NotReady); continuing")
            talosctl.reset(talosconfig, endpoint, address, control_plane=is_cp)
        else:
            # node_ready reads the kube Node: for a node with no kube Node there
            # is nothing to be Ready, so only consult it when a Node exists.
            if has_node:
                ready = kubectl.node_ready(kubeconfig, node)
                if ready is not False:
                    state = "Ready" if ready else "unknown"
                    raise ReconcileError(
                        f"no address for {node} but node is {state} in k8s; "
                        "aborting -- may be a discovery failure, not a reset node"
                    )
            # NotReady alone does not prove a control plane left etcd: a failed
            # or timed-out reset leaves a dead member, and deleting the VM would
            # bypass the reset-failure protection. Require positive proof the
            # member left: query the surviving control plane's AUTHORITATIVE etcd
            # member list -- NOT `get members` discovery data, which is not etcd
            # membership and even drops addressless entries. `etcd_members` fails
            # closed (raises) on a failed/ambiguous query, and otherwise we abort
            # unless the node is affirmatively absent from the live member list.
            if is_cp:
                etcd = talosctl.etcd_members(talosconfig, endpoint)
                if node in etcd:
                    raise ReconcileError(
                        f"no address for control plane {node} and it is still an "
                        f"etcd member (id {etcd[node]} on control plane {endpoint}); "
                        "NotReady does not prove it left etcd, aborting rather than "
                        "delete a member that could cost quorum"
                    )
                info(
                    f"no address for control plane {node} but it is absent from the "
                    f"surviving control plane's etcd member list; deleting"
                )
            elif has_node:
                warn(f"no address for {node} (node is NotReady, likely already reset); deleting")
            else:
                warn(f"no address for {node} and it has no kube node (never joined or "
                     "already removed); deleting the VM")
        if has_node:
            kubectl.delete_node(kubeconfig, node)
        backend.delete_machine(node, inv)
        removed += 1
        if is_cp:
            remaining_cp -= 1
            if remaining_cp > 0:
                info(f"control plane {node} removed; health-checking before the next")
                if not _health_or_kube_fallback(
                    talosconfig,
                    endpoint,
                    refs.kubernetes.vip,
                    kubeconfig,
                    timeout="10m",
                    fallback=False,
                ):
                    raise ReconcileError(
                        f"cluster unhealthy after removing control plane {node}; "
                        "refusing to remove another control plane (etcd quorum at risk)"
                    )
    if removed == 0:
        info("nothing to remove")


_SETTLE_GRACE_S = 120


def _apply_existing_configs(
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    configs: dict[str, str],
    talosconfig: Path,
    kubeconfig: Path,
    moving_from: str,
) -> None:
    """Push machine config to existing nodes, sequencing a kube-api endpoint move.

    When `moving_from` records an old endpoint, the control planes are
    reconfigured first and settled one at a time, then the kubeconfig is
    regenerated from a control plane (`_finish_endpoint_move`) -- the old
    endpoint dies with the old VIP, and the worker pass needs kubectl to see the
    nodes on the new one. Without a move, a single apply covers control planes
    and workers.
    """
    if moving_from:
        _apply_configs(
            cfg, machines, inv, refs, configs, talosconfig, kubeconfig,
            settle=True, roles=("controlplane",),
        )
        _finish_endpoint_move(cfg, refs, inv, talosconfig, kubeconfig)
        _apply_configs(
            cfg, machines, inv, refs, configs, talosconfig, kubeconfig, roles=("worker",)
        )
    else:
        _apply_configs(cfg, machines, inv, refs, configs, talosconfig, kubeconfig)


def _apply_configs(
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    configs: dict[str, str],
    talosconfig: Path,
    kubeconfig: Path,
    settle: bool = True,
    roles: tuple[str, ...] = ("controlplane", "worker"),
) -> None:
    """Push the freshly generated machine config to every existing node.

    `settle` runs control planes one at a time so a reboot-requiring patch never
    restarts every control plane at once: after each control plane's config is
    applied, its node is waited out of the cluster and back in (`_wait_down`
    then `_wait_reachable`) before the next one is touched. This is on by
    default because a reboot of two control planes together costs etcd quorum;
    the endpoint-move path relies on the same serialisation.

    Closes the gap where editing anything in the machine config (extra
    manifests, kubelet args, network) only reached NEW nodes, so a running
    cluster silently drifted from cluster.yaml.

    `apply_config` reports whether the apply restarted the node (`mode=auto`
    only restarts for a change that genuinely needs it), so a silent live/no-op
    apply skips the settle machinery entirely and costs nothing on a converged
    cluster. Only a restart-requiring patch takes a node down, and one is never
    touched again until that node has gone down, come back, and the cluster has
    passed `talosctl health` (apid reachability alone does not prove the node
    rejoined etcd). If the grace window expires without the node dropping, that
    is an unresolved reboot and converge refuses rather than advance past a
    control plane that may never have come back.
    """
    log("machine config")
    endpoint = _talos_endpoint(cfg, refs, inv, talosconfig)
    discovered = talosctl.member_addresses(
        talosconfig, endpoint, exclude_vip=_cluster_vips(cfg, refs, kubeconfig)
    )
    ordered = sorted(machines.items(), key=lambda kv: 0 if kv[1].role == "controlplane" else 1)
    applied = 0
    for host, _m in ordered:
        if _m.role not in roles or host not in inv.machines or host not in configs:
            continue
        if not kubectl.node_exists(kubeconfig, host):
            warn(f"{host}: not visible through {kubeconfig.name}, config not applied")
            continue
        address = resolve_node_address(host, discovered, inv, refs)
        if not address:
            continue
        reboot_pending = talosctl.apply_config(talosconfig, endpoint, address, configs[host])
        applied += 1
        if settle and _m.role == "controlplane" and not dry_run():
            if not reboot_pending:
                # a silent live/no-op apply never took the node down, so there
                # is no restart to settle and no quorum risk -- move on
                continue
            # wait for the node to actually go down (a reboot started), then for
            # apid to answer again, then for the cluster to be healthy -- so a
            # reboot-requiring patch is fully settled before the next control
            # plane is touched. apid reachability alone does not prove the node
            # rejoined etcd, so it is never treated as settled without health.
            if not _wait_down(talosconfig, address, address):
                raise ReconcileError(
                    f"{host}: apply requested a reboot but apid never dropped within "
                    f"the {_SETTLE_GRACE_S}s settle grace window; refusing to touch "
                    "the next control plane (a slow reboot cannot be told apart "
                    "from a stuck node)"
                )
            _wait_reachable(talosconfig, address, address)
            if not _health_or_kube_fallback(
                talosconfig,
                endpoint,
                refs.kubernetes.vip,
                kubeconfig,
                timeout="10m",
                fallback=False,
            ):
                raise ReconcileError(
                    f"cluster unhealthy after rebooting {host}; aborting config rollout"
                )
    if not applied:
        info("no existing nodes to configure")


def _wait_down(
    talosconfig: Path, endpoint: str, node: str, grace_s: int = _SETTLE_GRACE_S, interval_s: int = 5
) -> bool:
    """Wait for `node`'s apid to stop answering -- the signal a reboot-requiring
    config apply actually took the node down.

    apid keeps answering while Talos drains, so waiting for it to answer again
    (as `_wait_reachable` does on its own) can return before the node ever
    rebooted. Only a config change that genuinely needs a restart takes the node
    down; a silent live apply never does, so after `grace_s` without the node
    dropping this returns False. The caller treats a down as a reboot and waits
    for the node to come back; on False the caller cannot rule out a slow reboot
    and refuses to advance rather than risk the next control plane.
    """
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not talosctl.reachable(talosconfig, endpoint=endpoint, node=node):
            return True
        time.sleep(interval_s)
    return False


def _reconcile_talos(
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    installer_images: dict[tuple[str, ...], str],
    installer_schematics: dict[tuple[str, ...], str],
    talosconfig: Path,
    kubeconfig: Path,
) -> None:
    """Bring every existing, talos-reachable node to the target talos version
    and schematic (extension set), control planes first, health-checked between.

    Shared by the upgrade phase (pre-existing drift, before new nodes join) and
    the post-join reconcile: OpenStack first-boot and scaled-up nodes are created
    on the shared base image, so they finish converge a schematic short of the
    target unless they are reinstalled here after they join.
    """
    log(f"talos version (want {cfg.talos_version})")
    endpoint = _talos_endpoint(cfg, refs, inv, talosconfig)
    discovered = talosctl.member_addresses(
        talosconfig, endpoint, exclude_vip=_cluster_vips(cfg, refs, kubeconfig)
    )
    # controlplanes first
    ordered = sorted(machines.items(), key=lambda kv: 0 if kv[1].role == "controlplane" else 1)
    for host, m in ordered:
        if host not in inv.machines:
            continue
        if not kubectl.node_exists(kubeconfig, host):
            continue
        address = resolve_node_address(host, discovered, inv, refs)
        if not address:
            continue
        want_image = installer_images[m.extensions]
        want_schematic = installer_schematics[m.extensions]
        cur_ver = talosctl.server_version(talosconfig, endpoint, address)
        # the RUNNING schematic, not the installer reference in the machine
        # config (the apply phase has already rewritten the config to the target
        # by the time we get here), so an extension-only edit triggers a reinstall
        cur_schematic = talosctl.running_schematic(talosconfig, endpoint, address)
        # upgrade on a version change OR a schematic change (extension list edit);
        # an unreadable schematic is treated as matching, like an unreadable image
        # used to be, rather than forcing upgrades on nodes we cannot inspect
        if cur_ver == cfg.talos_version and (not cur_schematic or cur_schematic == want_schematic):
            # already at the target: a resumed run can reach this node without
            # ever health-checking it -- the previous run may have upgraded it
            # and died before etcd recovered, and its apid answers the whole
            # time. Before touching the next control plane, re-establish the
            # health barrier promised at the end of an upgrade.
            _uncordon_stale(kubeconfig, host)
            if m.role == "controlplane" and not _health_or_kube_fallback(
                talosconfig,
                endpoint,
                refs.kubernetes.vip,
                kubeconfig,
                timeout="10m",
                fallback=False,
            ):
                raise ReconcileError(f"cluster unhealthy before touching {host}; aborting rollout")
            info(f"{host}: {cur_ver or '?'}, ok")
            continue
        reason = "extensions changed" if cur_ver == cfg.talos_version else str(cur_ver or "?")
        info(f"{host}: {reason} -> {cfg.talos_version} ({want_image})")
        talosctl.upgrade(talosconfig, endpoint, address, want_image)
        _wait_version(talosconfig, endpoint, address, cfg.talos_version, want_schematic)
        _uncordon_stale(kubeconfig, host)
        if not _health_or_kube_fallback(
            talosconfig,
            endpoint,
            refs.kubernetes.vip,
            kubeconfig,
            timeout="10m",
            fallback=m.role != "controlplane",
        ):
            raise ReconcileError(f"cluster unhealthy after upgrading {host}; aborting rollout")


def _reconcile_joined(
    cfg: Config,
    machines: dict[str, Machine],
    backend,
    refs: NetworkResult,
    installer_images: dict[tuple[str, ...], str],
    installer_schematics: dict[tuple[str, ...], str],
    talosconfig: Path,
    kubeconfig: Path,
) -> InfrastructureInventory:
    """Refresh the inventory and reconcile every node's running schematic, then
    return the refreshed inventory.

    The compute phase creates machines through the backend without extending the
    pre-compute inventory `converge` loaded in the network phase, so a caller
    that reuses that stale inventory would skip every node it just created
    (`_reconcile_talos` only touches nodes it can see). Loading here guarantees
    OpenStack first-boot and scaled-up nodes -- which join on the shared base
    image -- are seen and reinstalled once they are up.
    """
    inv = backend.load_inventory()
    _reconcile_talos(
        cfg, machines, inv, refs, installer_images, installer_schematics, talosconfig, kubeconfig
    )
    return inv


def _upgrade(
    cfg: Config,
    machines: dict[str, Machine],
    inv: InfrastructureInventory,
    refs: NetworkResult,
    installer_images: dict[tuple[str, ...], str],
    installer_schematics: dict[tuple[str, ...], str],
    talosconfig: Path,
    kubeconfig: Path,
) -> None:
    """Roll existing nodes to the target talos and kubernetes versions before new
    nodes are created, so a new node never joins newer than the rest (see the
    converge docstring). Nodes created in the compute phase are caught again by
    `_reconcile_talos` in the health phase once they join.
    """
    _reconcile_talos(
        cfg, machines, inv, refs, installer_images, installer_schematics, talosconfig, kubeconfig
    )
    log(f"kubernetes version (want {cfg.kubernetes_version})")
    endpoint = _talos_endpoint(cfg, refs, inv, talosconfig)
    discovered = talosctl.member_addresses(
        talosconfig, endpoint, exclude_vip=_cluster_vips(cfg, refs, kubeconfig)
    )
    cur = kubectl.server_version(kubeconfig)
    if not cur:
        if dry_run() and not (kubeconfig.is_file() and kubeconfig.stat().st_size > 0):
            # plan prognosed the recovered cluster as up but wrote no kubeconfig,
            # so the running version is unknown and a real run recovers it and
            # steps it through the minors; there is nothing a dry run can upgrade,
            # and the 30s retry + stabilization waits have no kube-api to probe.
            info("kubernetes version unknown (missing kubeconfig); skipped in plan")
            return
        # the api server is briefly unreachable after a machine-config apply,
        # and an unknown current version costs us the minor-stepping path
        info("kube-api did not answer; retrying version check in 30s")
        time.sleep(30)
        cur = kubectl.server_version(kubeconfig)
    if cur == cfg.kubernetes_version:
        info(f"{cur}, ok")
        return
    cp1_address = next(
        (
            address
            for h, m in machines.items()
            if m.role == "controlplane"
            and (address := resolve_node_address(h, discovered, inv, refs))
        ),
        "",
    )
    if cp1_address:
        consecutive = 0
        for _ in range(12):
            if kubectl.cluster_up(kubeconfig):
                consecutive += 1
                if consecutive >= 2:
                    break
            else:
                consecutive = 0
            info("kube-api not yet stable after machine-config apply; retrying in 10s")
            time.sleep(10)
        else:
            raise ReconcileError("kube-api did not stabilize before k8s upgrade")
        cur = kubectl.server_version(kubeconfig)
        if not cur:
            raise ReconcileError("kube-api stabilized but server version is still unavailable")
        for step in _k8s_upgrade_path(cur, cfg.kubernetes_version):
            info(f"{cur or '?'} -> {step}")
            talosctl.upgrade_k8s(talosconfig, endpoint, cp1_address, step)
            cur = step
        # upgrade-k8s cordons each node in turn as it swaps the kubelet; a run
        # that was interrupted leaves that cordon behind on whichever node it
        # was working on
        for node in kubectl.unschedulable(kubeconfig):
            if node in machines:
                _uncordon_stale(kubeconfig, node)


# ---------------------------------------------------------------------------
# status + destroy
# ---------------------------------------------------------------------------


def status_report(root: Path) -> dict[str, Any]:
    """Everything `status` knows, as a plain dict.

    Split out of `status()` because it is also what a plugin is handed through
    `Context` -- the ingress VIP/floating ip and the OpenStack project live in
    OpenStack, not in cluster.yaml, and a plugin must not have to shell out to
    re-derive them.
    """
    cfg = load_config(root)
    secrets = load_secrets(root)
    backend = backend_for(cfg, secrets)
    inv = backend.load_inventory()
    refs = backend.current_network(inv)
    kubeconfig_path = root / "kubeconfig"

    advertised = refs.kubernetes.advertised_address
    kubeapi = {
        "floating_ip": advertised,
        "vip": refs.kubernetes.vip,
        "endpoint": f"https://{advertised}:6443" if advertised else "",
    }
    ingress = {
        "floating_ip": refs.ingress.advertised_address,
        "vip": refs.ingress.vip,
        "metallb": list(refs.metallb),
    }
    up = kubectl.cluster_up(kubeconfig_path)
    provider_status = backend.provider_status()

    return {
        "cluster": cfg.name,
        "infrastructure": {"provider": backend.name, **provider_status},
        "openstack": provider_status if backend.name == "openstack" else {},
        "kubernetes": kubeapi,
        "ingress": ingress,
        "resources": inv.resources,
        "nodes": kubectl.node_summary(kubeconfig_path) if up else [],
    }


def status(root: Path, output: str = "text") -> None:
    report = status_report(root)
    ctx = Context(root=root, cfg=load_config(root), status=report)
    plugin_reports = plugins.collect(plugins.active(ctx), "status", ctx)

    infrastructure = report["infrastructure"]
    kubeapi = report["kubernetes"]
    ingress = report["ingress"]
    api_url = kubeapi["endpoint"]

    if output == "yaml":
        print(yaml.safe_dump({**report, "plugins": plugin_reports}, sort_keys=False).rstrip())
        return

    log(f"status: {report['cluster']}")
    if infrastructure["provider"] == "openstack":
        info(
            f"openstack: {infrastructure['url']} "
            f"(region {infrastructure['region']}, "
            f"project {infrastructure['project'] or '?'})"
        )
    else:
        nodes = ", ".join(infrastructure.get("online_nodes", [])) or "none"
        info(f"proxmox: {infrastructure['url']} (online nodes: {nodes})")
    for kind, names in report["resources"].items():
        info(f"{kind}: {len(names)}")
        for n in names:
            info(f"    {n}")
    log("endpoints")
    info(f"kube api: {api_url or '(pending)'} (vip {kubeapi['vip'] or '(pending)'})")
    info(f"ingress:  {ingress['floating_ip'] or '(pending)'} (vip {ingress['vip'] or '(pending)'})")
    if report["nodes"]:
        print(kubectl.get_nodes_wide(root / "kubeconfig"))
    for name, data in plugin_reports.items():
        log(f"plugin: {name}")
        print_report(data)


def _running_versions(root: Path, cfg: Config) -> list[dict[str, Any]]:
    """Per-node {name, talos, kubernetes} as reported by the cluster itself.

    Deliberately local-only and cheap: ONE talos discovery call (which already
    carries each member's talos version) plus the local kubeconfig -- no
    OpenStack call and no per-node `talosctl version`, so a node whose apid is
    not answering directly is still reported at the version discovery knows.
    An empty version means "not known", never "wrong version".
    """
    talosconfig_path = root / "talosconfig"
    kubeconfig_path = root / "kubeconfig"
    endpoint = _talos_endpoint(cfg, talosconfig=talosconfig_path, required=False)

    up = kubectl.cluster_up(kubeconfig_path)
    kubelets = {
        n["name"]: n["version"] for n in (kubectl.node_summary(kubeconfig_path) if up else [])
    }
    # a cordon left behind by an interrupted upgrade is invisible in a version
    # comparison but breaks every health check, so report it here too
    cordoned = set(kubectl.unschedulable(kubeconfig_path)) if up else set()
    discovered = talosctl.members(talosconfig_path, endpoint) if talosconfig_path.is_file() else {}
    return [
        {
            "name": host,
            "talos": discovered[host].version if host in discovered else "",
            "kubernetes": kubelets.get(host, ""),
            "cordoned": host in cordoned,
        }
        for host in sorted(set(discovered) | set(kubelets))
    ]


def _component_check(name: str, configured: str, latest: str, latest_patch: str) -> dict[str, Any]:
    """One row of the version report: what cluster.yaml pins vs what upstream has.

    Two separate questions, because they have different answers:
      latest_patch  the newest patch of the SAME minor -- a safe, in-place bump
      latest        the newest release overall -- may cross a minor (for
                    kubernetes that means a multi-step upgrade, see
                    _k8s_upgrade_path)
    """
    return {
        "component": name,
        "configured": configured,
        "latest_patch": latest_patch,
        "latest": latest,
        "patch_available": bool(latest_patch) and versions.is_older(configured, latest_patch),
        "minor_available": bool(latest)
        and versions.is_older(configured, latest)
        and versions.minor(configured) != versions.minor(latest),
    }


def _incomplete_reasons(report: dict[str, Any], root: Path, cfg: Config) -> list[str]:
    """Why the check could not fully verify the cluster, or [] if it did.

    Covers both directions of missing data: an upstream release lookup that did
    not answer (empty newest fields), a running node whose version is unknown
    (empty talos/kubernetes in the report), and -- for an existing cluster -- a
    configured machine that shows up in neither Talos discovery nor the
    Kubernetes node list, so its versions cannot be verified at all. A cluster
    that should exist but answered nothing at all is also unverified; either
    client config file -- talosconfig or kubeconfig -- records that a cluster
    has been set up before.
    """
    reasons: list[str] = []
    for c in report["components"]:
        if not c["latest"]:
            reasons.append(f"{c['component']}: newest release unknown")
        elif not c["latest_patch"]:
            minor = versions.minor(c["configured"])
            reasons.append(f"{c['component']}: newest patch of {minor} unknown")
    for n in report["nodes"]:
        if not n["talos"]:
            reasons.append(f"node {n['name']}: talos version unknown")
        if not n["kubernetes"]:
            reasons.append(f"node {n['name']}: kubernetes version unknown")
    never_answered = not report["nodes"]
    setup_happened = (root / "talosconfig").is_file() or (root / "kubeconfig").is_file()
    if never_answered and setup_happened:
        reasons.append("cluster unreachable; no node versions known")
    # An existing cluster should run every configured machine. A node that
    # appears in neither Talos discovery nor the Kubernetes node list is
    # missing -- its versions are unverifiable, so the check must not pass as
    # current. Before a cluster exists (no talosconfig/kubeconfig yet) check
    # reports the pinned versions only, so missing machines are expected there
    # and are not called out.
    if setup_happened and not never_answered:
        expected = set(cfg.machines)
        observed = {n["name"] for n in report["nodes"]}
        for host in sorted(expected - observed):
            reasons.append(f"node {host} is missing from both Talos discovery and Kubernetes")
    return reasons


def check(root: Path, output: str = "text") -> int:
    """Compare cluster.yaml's pinned versions against the newest upstream
    releases (and against what the cluster actually runs).

    Read-only and cloud-free: it asks factory.talos.dev / dl.k8s.io what exists,
    talos discovery + the local kubeconfig what is running, and changes nothing.
    Returns 1 if an update, a drift, or an incomplete check (something could not
    be verified) was found, 0 only if everything is current and verified, so it
    can gate a CI job without passing an unverified cluster.
    """
    cfg = load_config(root)
    report: dict[str, Any] = {"cluster": cfg.name, "components": [], "nodes": []}
    ctx = Context(root=root, cfg=cfg)
    plugin_reports = plugins.collect(plugins.active(ctx), "check", ctx)

    try:
        talos_all = versions.talos_versions()
        talos_latest = versions.latest_talos(talos_all)
        talos_patch = versions.latest_talos_patch(versions.minor(cfg.talos_version), talos_all)
    except (requests.RequestException, ValueError) as e:
        warn(f"could not reach the talos image factory ({e}); talos not checked")
        talos_latest = talos_patch = ""
    try:
        k8s_latest = versions.latest_kubernetes()
        k8s_patch = versions.latest_kubernetes_patch(versions.minor(cfg.kubernetes_version))
    except (requests.RequestException, ValueError) as e:
        warn(f"could not reach dl.k8s.io ({e}); kubernetes not checked")
        k8s_latest = k8s_patch = ""

    report["components"] = [
        _component_check("talos", cfg.talos_version, talos_latest, talos_patch),
        _component_check("kubernetes", cfg.kubernetes_version, k8s_latest, k8s_patch),
    ]
    report["nodes"] = _running_versions(root, cfg)
    # a node running something other than cluster.yaml's pin: converge would fix it
    drift = [
        n
        for n in report["nodes"]
        if (n["talos"] and n["talos"] != cfg.talos_version)
        or (
            n["kubernetes"]
            and versions.parse(n["kubernetes"]) != versions.parse(cfg.kubernetes_version)
        )
    ]
    report["drift"] = [n["name"] for n in drift]
    cordoned = [n["name"] for n in report["nodes"] if n.get("cordoned")]
    report["cordoned"] = cordoned
    outdated = [c for c in report["components"] if c["patch_available"] or c["minor_available"]]
    # an incomplete check is not a clean bill of health: a missing upstream answer
    # or an unknown node version means we did not verify everything, so it must not
    # pass a CI gate as if it were up to date.
    incomplete_reasons = _incomplete_reasons(report, root, cfg)
    report["incomplete"] = bool(incomplete_reasons)
    report["incomplete_reasons"] = incomplete_reasons
    # a plugin that reports not-ok is a reason to exit 1, exactly like a drifted
    # node: converge would change something.
    report["plugins"] = plugin_reports
    plugins_ok = all(bool(r.get("ok")) for r in plugin_reports.values())
    report["up_to_date"] = (
        not outdated and not drift and not cordoned and plugins_ok and not report["incomplete"]
    )

    if output == "yaml":
        print(yaml.safe_dump(report, sort_keys=False).rstrip())
        return 0 if report["up_to_date"] else 1

    log(f"check: {cfg.name}")
    for c in report["components"]:
        info(
            f"{c['component']:<11} cluster.yaml {c['configured']:<10} "
            f"latest patch {c['latest_patch'] or '?':<10} "
            f"latest {c['latest'] or '?'}"
        )
    if report["nodes"]:
        log("running on the cluster")
        for n in report["nodes"]:
            info(
                f"{n['name']:<28} talos {n['talos'] or '(unknown)':<10} "
                f"kubelet {n['kubernetes'] or '(not joined)'}"
                f"{'   SchedulingDisabled' if n.get('cordoned') else ''}"
            )
    else:
        info("cluster not reachable; reporting cluster.yaml only")

    log("summary")
    for c in outdated:
        target = c["latest_patch"] if c["patch_available"] else c["latest"]
        key = "talos.version" if c["component"] == "talos" else "kubernetes.version"
        info(
            f"{c['component']}: {c['configured']} -> {target} available "
            f"(bump {key} in cluster.yaml, then `taloscluster converge`)"
        )
        if c["minor_available"] and c["latest"] != target:
            info(
                f"    newest {c['component']} is {c['latest']} "
                f"({versions.minor(c['latest'])} is a minor upgrade)"
            )
    if drift:
        info(
            f"{len(drift)} node(s) not on the configured versions "
            f"({', '.join(n['name'] for n in drift)}); `taloscluster converge` would upgrade them"
        )
    if cordoned:
        info(
            f"{len(cordoned)} node(s) cordoned ({', '.join(cordoned)}): nothing schedules "
            "there and `talosctl health` fails on them; `taloscluster converge` uncordons, "
            f"or `kubectl uncordon {cordoned[0]}`"
        )
    for name, data in plugin_reports.items():
        log(f"plugin: {name}")
        print_report(data)
    for reason in report["incomplete_reasons"]:
        warn(f"check incomplete: {reason}")
    if report["up_to_date"]:
        info("cluster.yaml pins the newest releases and every node is on them")
    elif report["incomplete"]:
        info("version check did not complete; nothing is assumed current")
    return 0 if report["up_to_date"] else 1


def dashboard(root: Path, nodes: list[str] | None = None) -> None:
    """Open `talosctl dashboard` on every node of the cluster.

    Three sources, in order of what each one knows:

      openstack   every machine taloscluster manages, including one that was just
                  created and has not booted talos yet
      discovery   the talos-level address of the ones that did boot
      apid probe  which of those actually answer right now

    The probe is not optional: `talosctl dashboard` fails fast if ANY target
    node is unreachable, so a single node mid-boot would otherwise take the
    whole dashboard down. Unreachable machines are reported and dropped.
    """
    cfg = load_config(root)
    talosconfig_path = root / "talosconfig"
    if not talosconfig_path.is_file():
        raise ReconcileError(f"missing {talosconfig_path} (run `taloscluster converge` first)")
    # cp-01's tailscale name or real address, the endpoint every talos call uses
    endpoint = _talos_endpoint(cfg, talosconfig=talosconfig_path)

    if nodes:
        targets = {n: n for n in nodes}
    else:
        backend = backend_for(cfg, load_secrets(root))
        inv = backend.load_inventory()
        refs = backend.current_network(inv)
        members = talosctl.member_addresses(
            talosconfig_path,
            endpoint,
            exclude_vip=_cluster_vips(cfg, refs, root / "kubeconfig"),
        )
        hosts = sorted(set(inv.machines) | set(members))
        if not hosts:
            raise ReconcileError(f"no nodes found for cluster {cfg.name}")
        targets = {h: resolve_node_address(h, members, inv) for h in hosts}

    log(f"dashboard: {cfg.name}")
    unknown = [h for h, addr in targets.items() if not addr]
    for host in unknown:
        warn(f"{host}: no address yet (not in talos discovery), skipping")

    known = {h: a for h, a in targets.items() if a}
    with ThreadPoolExecutor(max_workers=max(len(known), 1)) as pool:
        alive = dict(
            zip(
                known,
                pool.map(
                    lambda a: talosctl.reachable(talosconfig_path, endpoint, a), known.values()
                ),
                strict=True,
            )
        )
    for host, ok in alive.items():
        # explicit node arguments are their own label; discovered ones are named
        label = host if host == known[host] else f"{host}: {known[host]}"
        if ok:
            info(label)
        else:
            warn(f"{label} not answering, skipping")

    up = [known[h] for h, ok in alive.items() if ok]
    if not up:
        raise ReconcileError(
            f"no reachable nodes via {endpoint}. "
            + (
                "Is this machine on the tailnet?"
                if cfg.tailscale_enabled
                else "Can this machine reach the cluster network?"
            )
        )
    talosctl.dashboard(talosconfig_path, endpoint, up)


def print_env(root: Path) -> None:
    """Print the OS_* auth exports (from cluster.yaml + secrets.yaml) so the
    `openstack` CLI can use the same application credential taloscluster does:

        eval "$(taloscluster env)"
        openstack image show ...

    Note: this writes the credential secret to stdout -- intended for eval, not
    for logging.
    """
    cfg = load_config(root)
    secrets = load_secrets(root)
    backend_for(cfg, secrets).print_environment()


def image_download(root: Path) -> None:
    """Build (factory -> download -> decompress) and upload the boot image to
    Glance if it isn't there yet. Standalone version of the converge image phase;
    handy for pre-seeding the image without touching the cluster."""
    cfg = load_config(root)
    secrets = load_secrets(root)
    backend = backend_for(cfg, secrets)
    log("image download")
    name = backend.download_image()
    info(f"boot image: {name}")


def image_remove(root: Path, assume_yes: bool = False) -> None:
    """Delete the boot image for this cluster's talos version from Glance.

    converge never deletes the image (it is shared and reused); this is the
    explicit way to remove it, e.g. to force a rebuild after changing the baked
    base extensions.
    """
    cfg = load_config(root)
    secrets = load_secrets(root)
    backend_for(cfg, secrets).remove_image(assume_yes=assume_yes)


def destroy(root: Path, assume_yes: bool = False) -> int:
    cfg = load_config(root)
    secrets = load_secrets(root)
    backend = backend_for(cfg, secrets)
    inv = backend.load_inventory()

    log(f"destroy {cfg.name}: {backend.destroy_summary(inv)}")
    provider_label = "OpenStack" if backend.name == "openstack" else backend.name
    warn(
        f"this deletes all taloscluster-managed {provider_label} resources for this cluster "
        "(the shared boot image is NOT deleted), and removes talossecrets.yaml "
        "-- the cluster identity -- along with the talosconfig/kubeconfig derived "
        "from it. The next converge will be a brand-new cluster."
    )
    if not assume_yes and not dry_run():
        resp = input("type the cluster name to confirm: ").strip()
        if resp != cfg.name:
            raise SystemExit("aborted")

    # Plugins still run before OpenStack teardown, while the cluster is
    # reachable, but only after the user has confirmed the entire destroy.
    ctx = Context(root=root, cfg=cfg)
    failed = _run_plugins(ctx, "destroy", reverse=True, assume_yes=assume_yes)

    backend.destroy_resources(inv)

    # wipe local state (talossecrets.yaml plus the talosconfig/kubeconfig derived
    # from it; legacy bootstrapped marker is also cleaned up) so a later converge
    # starts a fresh cluster with a new identity. reset() honours --dry-run itself
    # so a plan still lists the files it would remove.
    State(root).reset()
    return failed
