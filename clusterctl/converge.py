"""The converger -- the heir of bin/cluster.sh.

Enforces the phase order the shell script established, the crux being that
existing nodes are upgraded to the target versions BEFORE new ones are added, so
a new node never joins newer than the rest:

  image -> secrets -> network/SG -> discover -> scale-down -> upgrade ->
  compute -> bootstrap -> kubeconfig -> health

State is not held in a file (except the talos secrets); every phase re-derives
what exists by querying OpenStack through the tagged inventory cache, so the
whole thing is safe to re-run.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openstack import exceptions as os_exceptions
from openstack.connection import Connection

from . import naming
from .config import Config, Machine, load_config, load_secrets, validate_warnings
from .errors import ReconcileError, preflight_tools
from .k8s import kubectl
from .openstack import compute, image, network, security
from .openstack.network import NetworkRefs, _fixed_ip
from .openstack.session import REGION, Inventory, connect
from .output import action, dry_run, info, log, warn
from .state import State
from .talos import factory, machineconfig, talosctl
from .talos.machineconfig import Endpoints


def converge(root: Path, assume_yes: bool = False) -> None:
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

    # installer image ref per extension set (schematic drives extension removal)
    installer_images = {
        s: factory.installer_image(factory.schematic_id(s), cfg.talos_version)
        for s in cfg.extension_sets()
    }

    conn = connect(cfg, secrets)

    # ---- 1. IMAGE --------------------------------------------------------
    log("image")
    boot_image = image.ensure_image(conn, cfg)

    # ---- 2. STATE (talos machine secrets) --------------------------------
    log("secrets")
    if not state.secrets_exist():
        action("generate talos machine secrets (first run)")
        if not dry_run():
            state.write_secrets(talosctl.gen_secrets(cfg.talos_version))
    secrets_path = state.secrets_path
    if state.secrets_exist():
        info(f"machine secrets: {secrets_path} (CRITICAL -- back this up)")

    # ---- 3. NETWORK + SECURITY -------------------------------------------
    log("network + security group")
    inv = Inventory(conn, cfg.name).load()
    sg = security.reconcile(conn, cfg, inv)
    refs = network.reconcile(conn, cfg, machines, inv, sg)
    info(f"kubeapi fip {refs.kubeapi_fip or '(pending)'} vip {refs.kubeapi_vip or '(pending)'}")

    # write the client talosconfig now that the endpoint (fip) is known. cp-01's
    # tailscale name goes in as the context endpoint so a hand-typed `talosctl`
    # needs no -e; -n stays mandatory. clusterctl itself still passes both.
    if state.secrets_exist() and refs.kubeapi_fip and not dry_run():
        talosconfig_path.write_text(
            talosctl.gen_talosconfig(
                cfg.name, refs.kubeapi_fip, secrets_path,
                client_endpoint=f"{cfg.name}-controlplane-01",
            )
        )
        os.chmod(talosconfig_path, 0o600)

    ep = Endpoints(kubeapi_fip=refs.kubeapi_fip, kubeapi_vip=refs.kubeapi_vip)

    # ---- machine configs (need the fip/vip from the network phase) -------
    configs: dict[str, str] = {}
    if state.secrets_exist() and refs.kubeapi_fip:
        configs = machineconfig.build_configs(
            cfg, secrets, machines, ep, secrets_path, installer_images
        )

    # ---- 4. DISCOVER: is the cluster reachable? --------------------------
    # The only robust "needs bootstrap" signal is that the kube-api does not
    # answer. We don't trust a persisted marker (survives destroy) or "servers
    # exist" (servers can exist un-bootstrapped, e.g. a create that didn't reach
    # bootstrap). bootstrap itself is idempotent -- on an already-bootstrapped
    # cluster it reports "already bootstrapped" and we treat that as success --
    # so attempting it whenever the cluster is down is safe.
    up = kubectl.cluster_up(kubeconfig_path)
    info(f"cluster {'UP' if up else 'not up (will bootstrap if needed)'}")

    # ---- 5. SCALE-DOWN ---------------------------------------------------
    if up:
        _scale_down(conn, cfg, machines, inv, refs, talosconfig_path, kubeconfig_path)

    # ---- 6. MACHINE CONFIG (existing nodes) ------------------------------
    # Before the upgrade phase on purpose: cluster.extraManifests lives in the
    # machine config, and `talosctl upgrade-k8s` refuses to finish until every
    # bootstrap manifest reconciles -- so a manifest fix has to land first.
    if up and configs:
        _apply_configs(cfg, machines, inv, refs, configs, talosconfig_path, kubeconfig_path)

    # ---- 7. UPGRADE (before adding new nodes) ----------------------------
    if up:
        _upgrade(cfg, machines, inv, refs, installer_images, talosconfig_path, kubeconfig_path)

    # ---- 7. COMPUTE (create / scale up) ----------------------------------
    log("compute")
    if configs or dry_run():
        compute.reconcile(conn, cfg, machines, inv, boot_image, configs)
    else:
        warn("skipping compute: no machine configs (network fip not ready)")

    # talosctl control operations go through cp-01's tailscale name (this host
    # must be on the tailnet anyway), which is always reachable -- unlike the
    # kube-api floating ip, whose routing from this host isn't guaranteed. The
    # VIP is the talos "node"; the fip stays the kube-api server URL in the
    # kubeconfig.
    cp1 = f"{cfg.name}-controlplane-01"

    # ---- 8. BOOTSTRAP (if the cluster isn't up) --------------------------
    if not up and not dry_run():
        log("bootstrap")
        # a freshly created node must boot, start tailscale, and register with
        # headscale before its name resolves -- wait for it (pre-VIP: node=cp1)
        _wait_reachable(talosconfig_path, cp1, cp1)
        # idempotent: on an already-bootstrapped cluster this is a no-op
        talosctl.bootstrap(talosconfig_path, endpoint=cp1, node=cp1)

    # ---- 9. KUBECONFIG ---------------------------------------------------
    if state.secrets_exist() and refs.kubeapi_vip and not dry_run():
        log("kubeconfig")
        # wait for the VIP to be announced (controlplane up post-bootstrap)
        _wait_reachable(talosconfig_path, cp1, refs.kubeapi_vip)
        talosctl.kubeconfig(talosconfig_path, cp1, refs.kubeapi_vip, kubeconfig_path)

    # ---- 10. HEALTH + STATUS ---------------------------------------------
    if not dry_run() and refs.kubeapi_vip:
        log("health")
        _health_or_kube_fallback(talosconfig_path, cp1, refs.kubeapi_vip, kubeconfig_path)
        log("status")
        print(kubectl.get_nodes_wide(kubeconfig_path))
        print(f"kube api:   https://{refs.kubeapi_fip}:6443")
        print(f"ingress ip: {refs.ingress_fip} (reserved)")
        print(f"talosctl:   talosctl --talosconfig {talosconfig_path} "
              f"-e {cp1} -n {refs.kubeapi_vip} <cmd>")
        print(f"kubectl:    kubectl --kubeconfig {kubeconfig_path} get nodes")


# ---------------------------------------------------------------------------
# phases that need cross-resource reasoning
# ---------------------------------------------------------------------------

def _wait_reachable(talosconfig: Path, endpoint: str, node: str,
                    timeout_s: int = 900, interval_s: int = 15) -> None:
    """Block until talos apid answers (endpoint -> node), or time out.

    Used both to wait for a fresh node to join the tailnet before bootstrap
    (endpoint=node=cp-01) and to wait for the VIP to be announced after bootstrap
    (endpoint=cp-01, node=VIP). Requires this machine to be on the tailnet.
    """
    info(f"waiting for {endpoint} -> {node} to become reachable "
         f"(up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if talosctl.reachable(talosconfig, endpoint=endpoint, node=node):
            info(f"{endpoint} -> {node} is reachable")
            return
        time.sleep(interval_s)
    raise TimeoutError(
        f"{endpoint} -> {node} did not become reachable within {timeout_s // 60}m. "
        "Is this machine on the tailnet, and is there a stale headscale entry "
        f"for {endpoint}? (see README: headscale hygiene)"
    )


def _wait_version(talosconfig: Path, endpoint: str, node: str, want: str,
                  timeout_s: int = 1800, interval_s: int = 10) -> None:
    """Block until `node` reboots into talos `want`.

    Replaces `talosctl upgrade --wait`, whose watch stream dies with
    ENHANCE_YOUR_CALM/too_many_pings whenever the client is newer than the
    server -- always true mid-upgrade (see talosctl.upgrade). Polling is
    immune to that, and to the node dropping off the network while it reboots.

    30m matches talosctl's own upgrade timeout: the node has to pull the
    installer image from factory.talos.dev before it can reboot, and a slow or
    flaky pull is the normal reason this takes a while. A timeout here means
    the upgrade really did not land -- check `talosctl -n <node> dmesg` for
    image-pull errors.
    """
    if dry_run():
        return
    info(f"waiting for {node} to come back on {want} (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    seen = ""
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        try:
            seen = talosctl.server_version(talosconfig, endpoint, node)
        except subprocess.CalledProcessError:
            continue  # node is rebooting; apid not answering yet
        if seen == want:
            info(f"{node} is on {want}")
            return
    raise TimeoutError(
        f"{node} did not come back on {want} within {timeout_s // 60}m "
        f"(last seen: {seen or 'unreachable'}). Check `talosctl -n {node} dmesg`."
    )


def _latest_patch(minor: str) -> str:
    """Newest patch release of a kubernetes minor, e.g. "1.35" -> "v1.35.7"."""
    with urllib.request.urlopen(  # noqa: S310 - fixed https host
        f"https://dl.k8s.io/release/stable-{minor}.txt", timeout=15
    ) as resp:
        return resp.read().decode().strip()


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
            path.append(_latest_patch(label))
        except OSError as e:
            warn(f"could not resolve latest {label} patch ({e}); using {label}.0")
            path.append(f"v{label}.0")
    path.append(want)
    if len(path) > 1:
        info(f"stepping through minors: {' -> '.join(path)}")
    return path


def _health_or_kube_fallback(talosconfig: Path, endpoint: str, vip: str,
                             kubeconfig: Path, timeout: str = "5m") -> bool:
    """talosctl health, falling back to kube-api readiness on failure.

    Retried once, because the check is aimed at the VIP and the VIP relocates
    to another control plane whenever one reboots -- so a rolling upgrade tends
    to reset the health check's own connection:

        healthcheck error: ... read tcp ...->VIP:50000: connection reset by peer

    That is the failover working, not a sick cluster; by the retry the VIP has
    settled on its new owner.

    Returns True if either signal says the cluster is usable.
    """
    for attempt in (1, 2):
        try:
            # k8s_endpoint=vip: the server-side check runs on the node, which cannot
            # reach its own floating ip (no NAT hairpin) -- see talosctl.health()
            talosctl.health(talosconfig, endpoint, vip, timeout=timeout, k8s_endpoint=vip)
            return True
        except subprocess.CalledProcessError:
            if attempt == 1:
                info("health check interrupted (VIP failover?); retrying in 30s")
                time.sleep(30)

    # talosctl health probes every node's apid via cluster discovery; it can
    # still fail on a transient tailnet/discovery hiccup even when the cluster
    # is fine. Fall back to the kubernetes readiness signal.
    warn("talosctl health did not pass twice; checking kubernetes readiness instead")
    if kubectl.cluster_up(kubeconfig):
        info("kube-api is up and answering -- cluster is usable")
        return True
    warn("kube-api is not answering either -- investigate the cluster")
    return False


def _private_ip(inv: Inventory, refs: NetworkRefs | None, host: str) -> str:
    if refs is not None and host in refs.machine_private_ips:
        return refs.machine_private_ips[host]
    return _fixed_ip(inv.get("ports", host))


def _scale_down(conn: Connection, cfg: Config, machines: dict[str, Machine],
                inv: Inventory, refs: NetworkRefs, talosconfig: Path,
                kubeconfig: Path) -> None:
    log("scale down")
    # talosctl endpoint = cp-01's tailscale name (reliably reachable from this
    # tailnet host); the node is always a numeric private ip apid can route to.
    endpoint = f"{cfg.name}-controlplane-01"
    desired = set(machines)
    live = kubectl.node_names(kubeconfig)
    removed = 0
    for node in live:
        if node in desired:
            continue
        is_cp = "-controlplane-" in node
        if is_cp:
            desired_cp = int(cfg.controlplane["count"])
            if desired_cp % 2 == 0 or desired_cp < 1:
                raise ReconcileError(
                    f"refusing to remove controlplane {node}: desired controlplane "
                    f"count {desired_cp} would break etcd quorum"
                )
        ip = _private_ip(inv, refs, node)
        if not ip:
            warn(f"no private ip for {node}, skipping removal")
            continue
        info(f"removing {node} ({ip})")
        kubectl.drain(kubeconfig, node)
        talosctl.reset(talosconfig, endpoint, ip)
        kubectl.delete_node(kubeconfig, node)
        compute.delete_node(conn, node, inv)
        removed += 1
    if removed == 0:
        info("nothing to remove")


def _apply_configs(cfg: Config, machines: dict[str, Machine], inv: Inventory,
                   refs: NetworkRefs, configs: dict[str, str],
                   talosconfig: Path, kubeconfig: Path) -> None:
    """Push the freshly generated machine config to every existing node.

    Closes the gap where editing anything in the machine config (extra
    manifests, kubelet args, network) only reached NEW nodes, so a running
    cluster silently drifted from cluster.yaml.

    Applying an identical config is a no-op on the node, so this stays cheap
    and idempotent on a converged cluster. mode=auto means talos reboots a node
    only for a change that genuinely requires it.
    """
    log("machine config")
    endpoint = f"{cfg.name}-controlplane-01"
    ordered = sorted(machines.items(), key=lambda kv: 0 if kv[1].role == "controlplane" else 1)
    applied = 0
    for host, _m in ordered:
        if not inv.get("servers", host) or host not in configs:
            continue
        if not kubectl.node_exists(kubeconfig, host):
            continue
        ip = refs.machine_private_ips.get(host, "")
        if not ip:
            continue
        talosctl.apply_config(talosconfig, endpoint, ip, configs[host])
        applied += 1
    if not applied:
        info("no existing nodes to configure")


def _upgrade(cfg: Config, machines: dict[str, Machine], inv: Inventory,
             refs: NetworkRefs, installer_images: dict[tuple[str, ...], str],
             talosconfig: Path, kubeconfig: Path) -> None:
    log(f"talos version (want {cfg.talos_version})")
    endpoint = f"{cfg.name}-controlplane-01"
    # controlplanes first
    ordered = sorted(machines.items(), key=lambda kv: 0 if kv[1].role == "controlplane" else 1)
    for host, m in ordered:
        if not inv.get("servers", host):
            continue
        if not kubectl.node_exists(kubeconfig, host):
            continue
        ip = refs.machine_private_ips.get(host, "")
        if not ip:
            continue
        want_image = installer_images[m.extensions]
        cur_ver = talosctl.server_version(talosconfig, endpoint, ip)
        cur_image = talosctl.node_image(talosconfig, endpoint, ip)
        # upgrade on a version change OR a schematic change (extension list edit)
        if cur_ver == cfg.talos_version and (not cur_image or cur_image == want_image):
            info(f"{host}: {cur_ver or '?'}, ok")
            continue
        info(f"{host}: {cur_ver or '?'} -> {cfg.talos_version} ({want_image})")
        talosctl.upgrade(talosconfig, endpoint, ip, want_image)
        _wait_version(talosconfig, endpoint, ip, cfg.talos_version)
        if not _health_or_kube_fallback(talosconfig, endpoint, refs.kubeapi_vip,
                                        kubeconfig, timeout="10m"):
            raise ReconcileError(f"cluster unhealthy after upgrading {host}; aborting rollout")

    log(f"kubernetes version (want {cfg.kubernetes_version})")
    cur = kubectl.server_version(kubeconfig)
    if not cur:
        # the api server is briefly unreachable after a machine-config apply,
        # and an unknown current version costs us the minor-stepping path
        info("kube-api did not answer; retrying version check in 30s")
        time.sleep(30)
        cur = kubectl.server_version(kubeconfig)
    if cur == cfg.kubernetes_version:
        info(f"{cur}, ok")
        return
    cp1_ip = next(
        (refs.machine_private_ips[h] for h, m in machines.items()
         if m.role == "controlplane" and h in refs.machine_private_ips),
        "",
    )
    if cp1_ip:
        for step in _k8s_upgrade_path(cur, cfg.kubernetes_version):
            info(f"{cur or '?'} -> {step}")
            talosctl.upgrade_k8s(talosconfig, endpoint, cp1_ip, step)
            cur = step


# ---------------------------------------------------------------------------
# status + destroy
# ---------------------------------------------------------------------------

def status(root: Path) -> None:
    cfg = load_config(root)
    secrets = load_secrets(root)
    conn = connect(cfg, secrets)
    inv = Inventory(conn, cfg.name).load()
    log(f"status: {cfg.name}")
    for kind in ("networks", "subnets", "routers", "security_groups", "ports", "ips", "servers"):
        names = sorted(inv.all(kind))
        info(f"{kind}: {len(names)}")
        for n in names:
            info(f"    {n}")
    kubeconfig_path = root / "kubeconfig"
    if kubectl.cluster_up(kubeconfig_path):
        print(kubectl.get_nodes_wide(kubeconfig_path))


def dashboard(root: Path, nodes: list[str] | None = None) -> None:
    """Open `talosctl dashboard` on every node of the cluster.

    Three sources, in order of what each one knows:

      openstack   every machine clusterctl manages, including one that was just
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
        raise ReconcileError(f"missing {talosconfig_path} (run `clusterctl converge` first)")
    # cp-01's tailscale name, the endpoint every other talos call here uses
    endpoint = f"{cfg.name}-controlplane-01"

    if nodes:
        targets = {n: n for n in nodes}
    else:
        conn = connect(cfg, load_secrets(root))
        inv = Inventory(conn, cfg.name).load()
        members = talosctl.member_addresses(talosconfig_path, endpoint)
        hosts = sorted(set(inv.all("servers")) | set(members))
        if not hosts:
            raise ReconcileError(f"no nodes found for cluster {cfg.name}")
        targets = {h: members.get(h) or _private_ip(inv, None, h) for h in hosts}

    log(f"dashboard: {cfg.name}")
    unknown = [h for h, addr in targets.items() if not addr]
    for host in unknown:
        warn(f"{host}: no address yet (not in talos discovery), skipping")

    known = {h: a for h, a in targets.items() if a}
    with ThreadPoolExecutor(max_workers=max(len(known), 1)) as pool:
        alive = dict(zip(
            known,
            pool.map(lambda a: talosctl.reachable(talosconfig_path, endpoint, a), known.values()),
            strict=True,
        ))
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
            f"no reachable nodes via {endpoint}. Is this machine on the tailnet?"
        )
    talosctl.dashboard(talosconfig_path, endpoint, up)


def print_env(root: Path) -> None:
    """Print the OS_* auth exports (from cluster.yaml + secrets.yaml) so the
    `openstack` CLI can use the same application credential clusterctl does:

        eval "$(clusterctl env)"
        openstack image show ...

    Note: this writes the credential secret to stdout -- intended for eval, not
    for logging.
    """
    cfg = load_config(root)
    secrets = load_secrets(root)
    print(f"export OS_AUTH_URL={shlex.quote(cfg.openstack_url)}")
    print("export OS_AUTH_TYPE=v3applicationcredential")
    print(f"export OS_REGION_NAME={shlex.quote(REGION)}")
    print(f"export OS_APPLICATION_CREDENTIAL_ID={shlex.quote(secrets.openstack_credential_id)}")
    secret = shlex.quote(secrets.openstack_credential_secret)
    print(f"export OS_APPLICATION_CREDENTIAL_SECRET={secret}")


def image_download(root: Path) -> None:
    """Build (factory -> download -> decompress) and upload the boot image to
    Glance if it isn't there yet. Standalone version of the converge image phase;
    handy for pre-seeding the image without touching the cluster."""
    cfg = load_config(root)
    secrets = load_secrets(root)
    conn = connect(cfg, secrets)
    log("image download")
    name = image.ensure_image(conn, cfg)
    info(f"boot image: {name}")


def image_remove(root: Path, assume_yes: bool = False) -> None:
    """Delete the boot image for this cluster's talos version from Glance.

    converge never deletes the image (it is shared and reused); this is the
    explicit way to remove it, e.g. to force a rebuild after changing the baked
    base extensions.
    """
    cfg = load_config(root)
    secrets = load_secrets(root)
    conn = connect(cfg, secrets)
    name = naming.image_name(cfg.talos_version)
    img = conn.image.find_image(name)
    if img is None:
        info(f"image {name} not found, nothing to remove")
        return
    log(f"remove image {name}")
    warn("other clusters on the same talos version may share this image")
    if not assume_yes and not dry_run():
        resp = input(f"type '{name}' to confirm: ").strip()
        if resp != name:
            raise SystemExit("aborted")
    action(f"delete image {name}")
    if not dry_run():
        try:
            conn.image.delete_image(img.id)
        except os_exceptions.SDKException as e:
            raise RuntimeError(
                f"could not delete image {name}: {e}\n"
                "On Ceph-backed clouds (like Radiant) each boot volume is a "
                "copy-on-write clone of the image, so the image cannot be deleted "
                "while any cluster's nodes still exist. Note: you usually do NOT "
                "need to delete the image -- `clusterctl image download` updates "
                "its properties in place. To rebuild it, `destroy` the dependent "
                "cluster(s) first, then `image remove`."
            ) from e


def destroy(root: Path, assume_yes: bool = False) -> None:
    cfg = load_config(root)
    secrets = load_secrets(root)
    conn = connect(cfg, secrets)
    inv = Inventory(conn, cfg.name).load()

    servers = inv.all("servers")
    ports = inv.all("ports")
    fips = inv.all("ips")
    log(f"destroy {cfg.name}: {len(servers)} servers, {len(ports)} ports, "
        f"{len(fips)} floating ips, network + router + security group")
    warn("this deletes all clusterctl-managed OpenStack resources for this cluster "
         "(the shared boot image is NOT deleted), and removes talossecrets.yaml "
         "-- the cluster identity -- along with the talosconfig/kubeconfig derived "
         "from it. The next converge will be a brand-new cluster.")
    if not assume_yes and not dry_run():
        resp = input("type the cluster name to confirm: ").strip()
        if resp != cfg.name:
            raise SystemExit("aborted")

    for host in list(servers):
        compute.delete_node(conn, host, inv)
    for name, fip in list(fips.items()):
        action(f"delete floating ip {name}")
        if not dry_run():
            conn.network.delete_ip(fip.id)
    # delete every remaining managed port (the reserved kubeapi/ingress ports
    # hold IP allocations in the subnet, so they must go before it)
    for name, port in list(inv.all("ports").items()):
        action(f"delete port {name}")
        if not dry_run():
            try:
                conn.network.delete_port(port.id)
            except os_exceptions.SDKException as e:
                warn(f"could not delete port {name}: {e}")
        inv.drop("ports", name)
    managed_subnets = list(inv.all("subnets").values())
    for name, rtr in list(inv.all("routers").items()):
        action(f"delete router {name}")
        if not dry_run():
            for sub in managed_subnets:
                try:
                    conn.network.remove_interface_from_router(rtr, subnet=sub.id)
                except os_exceptions.SDKException as e:
                    warn(f"could not detach subnet from router {name}: {e}")
            conn.network.delete_router(rtr.id)
    for name, sub in list(inv.all("subnets").items()):
        action(f"delete subnet {name}")
        if not dry_run():
            conn.network.delete_subnet(sub.id)
    for name, net in list(inv.all("networks").items()):
        action(f"delete network {name}")
        if not dry_run():
            conn.network.delete_network(net.id)
    for name, sg in list(inv.all("security_groups").items()):
        action(f"delete security group {name}")
        if not dry_run():
            conn.network.delete_security_group(sg.id)

    # wipe local state (talossecrets.yaml plus the talosconfig/kubeconfig derived
    # from it; legacy bootstrapped marker is also cleaned up) so a later converge
    # starts a fresh cluster with a new identity. reset() honours --dry-run itself
    # so a plan still lists the files it would remove.
    State(root).reset()
