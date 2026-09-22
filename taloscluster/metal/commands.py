"""The `taloscluster metal` commands: joining bare-metal machines.

The flow mirrors the prototype this provider was ported from. `boot` mounts
the factory's install ISO in the machine's virtual media, one-time boots from
it and powers the machine on (`--serve` hands the ISO out over the LAN for a
BMC with no internet egress); `wait` polls for the maintenance-mode apid on
the machine's cluster address; `apply` generates the machine config and pushes
it to the maintenance-mode node; `eject` unmounts the media; `verify` waits
for the node to come back with its configuration. `join` runs the five in
order, and `inspect` prints a Redfish summary of power, boot, NICs and disks.
A machine whose redfish is off never touches its BMC: `join` becomes wait,
apply and verify, and the BMC-only commands skip with a notice.

The BMC is only ever asked to mount media, one-time boot it and manage power:
no BIOS boot-mode changes and no boot-order manipulation. After the one-time
boot the machine falls back to its own order, which for an installed machine
is its disk.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from ..config import Config, ConfigError, MetalServer, load_config
from ..converge import _config_kubernetes_version
from ..errors import ReconcileError
from ..naming import METAL_BASE_EXTENSIONS
from ..output import action, info, report, warn
from ..state import State
from ..talos import factory, talosctl
from . import redfish
from . import talos as metal_talos

# ISO boot to a maintenance-mode apid: the BMC fetches the media, the machine
# boots it and Talos starts apid with no configuration applied yet. How long
# that is allowed to take is the machine's `boot_timeout`.
# apply-config to a node booted from the ISO installs Talos to disk and
# reboots into it, which takes longer than a plain boot
VERIFY_TIMEOUT_S = 1200


# -- shared lookups -----------------------------------------------------------


def _find_server(cfg: Config, name: str) -> MetalServer:
    """The machine `name` from the `metal:` section, whoever's group it is in."""
    metal = cfg.metal
    if metal is None:
        raise ConfigError("cluster.yaml has no metal section")
    for group in metal.groups.values():
        if name in group.servers:
            return group.servers[name]
    known = sorted(s for g in metal.groups.values() for s in g.servers)
    raise ConfigError(
        f"cluster.yaml has no metal server named {name!r} "
        f"(known: {', '.join(known) or 'none'})"
    )


def _no_bmc(server: MetalServer) -> None:
    """The notice that a machine's BMC is left alone: its redfish is off."""
    info(
        f"metal server {server.name} has redfish disabled, so its BMC is "
        "never touched; boot the machine into maintenance mode yourself"
    )


def _bmc(server: MetalServer) -> redfish.Redfish | None:
    """The Redfish client for one machine's controller.

    A machine whose redfish is off never touches its BMC: the caller gets
    None after the notice, and skips whatever it needed the BMC for. The
    loader refuses a `redfish: true` machine without a bmc.ip, so a client
    is always constructible here.
    """
    if not server.redfish:
        _no_bmc(server)
        return None
    return redfish.Redfish(server.bmc)


def _cluster_ip(server: MetalServer) -> str:
    """The static address on the machine's cluster link, where apid answers."""
    return metal_talos.cluster_ip(server)


def _iso_url(cfg: Config) -> str:
    """The factory's install ISO for the cluster's Talos version, with the metal
    base extensions baked in.

    Not the VM providers' boot image: that one carries qemu-guest-agent, whose
    service never starts on bare metal and leaves the machine blocked in
    `startAllServices` instead of reaching the maintenance apid.
    """
    schematic = factory.schematic_id(METAL_BASE_EXTENSIONS)
    return factory.nocloud_iso_url(schematic, cfg.talos_version)


def _installer_image(cfg: Config) -> str:
    """The metal installer ref for the machine's resolved extension set."""
    return metal_talos.installer(cfg)[1]


# -- the commands ---------------------------------------------------------------


def inspect(root: Path, name: str) -> None:
    """Print a Redfish summary of one machine's power, boot, NICs and disks."""
    cfg = load_config(root)
    server = _find_server(cfg, name)
    rf = _bmc(server)
    if rf is None:
        return
    report({name: rf.summary()})


def boot(root: Path, name: str, *, serve: bool = False, foreground: bool = True) -> None:
    """Mount the install ISO as virtual media, one-time boot it, power on.

    With `serve` the ISO is downloaded and handed out from this machine over
    the LAN, for a BMC with no internet egress; the server lives until the
    process exits, and a standalone `boot --serve` (`foreground`) waits for
    Ctrl-C so the BMC can keep fetching while the machine boots. `join` calls
    this with `foreground=False` and keeps serving while it waits and applies.
    """
    cfg = load_config(root)
    server = _find_server(cfg, name)
    rf = _bmc(server)
    if rf is None:
        return
    _refuse_joined(root, server)
    iso_url = _iso_url(cfg)
    if rf.eject_media():
        info(f"ejected the media already mounted on {server.bmc.ip}")
    if serve:
        media = _ServedIso(iso_url)
        iso_url = media.url_for(_local_address_for(server.bmc.ip))
    action(f"mount {iso_url} as the virtual media of {server.bmc.ip}")
    rf.insert_media(iso_url)
    action(f"one-time boot {server.bmc.ip} from the virtual media")
    rf.boot_once_cd()
    action(f"power on {server.bmc.ip}")
    rf.power_on()
    info(f"install media: {iso_url}")
    if serve and foreground:
        info("serving the ISO from this machine; press Ctrl-C once the machine has booted")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            info("stopped serving the ISO")
            raise


def wait(root: Path, name: str, *, timeout_s: int | None = None,
         interval_s: int = 10) -> None:
    """Poll for the maintenance-mode apid on the machine's cluster address.

    The budget is the machine's `boot_timeout`, so a group of cold-booting
    hardware raises it once for every machine in it; `timeout_s` overrides it.
    """
    cfg = load_config(root)
    server = _find_server(cfg, name)
    if timeout_s is None:
        timeout_s = server.boot_timeout
    ip = _cluster_ip(server)
    info(f"waiting for the maintenance apid on {ip} (up to {timeout_s // 60}m)...")
    deadline = time.monotonic() + timeout_s
    while not talosctl.maintenance_reachable(ip):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{name} did not answer the maintenance apid on {ip} within "
                f"{timeout_s // 60}m; is the machine booted from the install "
                "media and reachable on the cluster network?"
            )
        time.sleep(interval_s)
    info(f"{name} is up in maintenance mode on {ip}")


def _warn_unignored(root: Path) -> None:
    """Warn when the cluster directory's .gitignore does not cover `.metal/`.

    Clusters scaffolded before the entry was added to the scaffold lack it,
    and the generated machine config carries the cluster's credentials.
    """
    gitignore = root / ".gitignore"
    present = (
        {line.strip() for line in gitignore.read_text().splitlines()}
        if gitignore.is_file()
        else set()
    )
    if ".metal/" not in present:
        warn(
            f"{gitignore} does not ignore .metal/: the generated machine config "
            "carries the cluster's credentials -- add .metal/ to it"
        )


def apply(root: Path, name: str) -> None:
    """Generate the machine config and push it to the maintenance-mode node.

    The generated config is kept at `.metal/<name>-<role>.yaml` in the cluster
    directory: it carries the cluster's credentials, so it is written mode 0600
    and the scaffold keeps `.metal/` out of git. A directory scaffolded before
    the entry existed gets a warning until its .gitignore covers it.

    On a bootstrapped cluster the config bakes the RUNNING kubernetes version,
    through the same helper converge's machine-config phase uses: applying the
    target would start a kubelet newer than the API server and pull the target
    kube-proxy before `talosctl upgrade-k8s` stepped the minors. A cluster with
    no kubeconfig yet has no running version, so it gets the target.
    """
    cfg = load_config(root)
    server = _find_server(cfg, name)
    _refuse_joined(root, server)
    secrets = State(root).require_secrets()
    kubeconfig = root / "kubeconfig"
    # a non-empty kubeconfig is the bootstrap signal converge itself uses
    bootstrapped = kubeconfig.is_file() and kubeconfig.stat().st_size > 0
    config_yaml = metal_talos.build_config(
        server, cfg, secrets, _installer_image(cfg),
        kubernetes_version=_config_kubernetes_version(cfg, kubeconfig, bootstrapped),
    )
    out_dir = root / ".metal"
    _warn_unignored(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}-{server.role}.yaml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(config_yaml)
    info(f"machine config: {path}")
    talosctl.apply_config_insecure(_cluster_ip(server), config_yaml)


def eject(root: Path, name: str) -> None:
    """Unmount the machine's virtual media."""
    cfg = load_config(root)
    server = _find_server(cfg, name)
    rf = _bmc(server)
    if rf is None:
        return
    action(f"eject the virtual media of {server.bmc.ip}")
    if rf.eject_media():
        info(f"virtual media ejected from {server.bmc.ip}")
    else:
        info(f"no virtual media mounted on {server.bmc.ip}")


def verify(root: Path, name: str, *, timeout_s: int = VERIFY_TIMEOUT_S,
           interval_s: int = 15) -> None:
    """Wait for the node to leave maintenance mode and answer as configured."""
    cfg = load_config(root)
    server = _find_server(cfg, name)
    ip = _cluster_ip(server)
    talosconfig = root / "talosconfig"
    if not talosconfig.is_file():
        # no client config yet (a cluster's first node): derive a throwaway one
        # from the machine secrets for the check; converge writes the real one
        secrets = State(root).require_secrets()
        with tempfile.TemporaryDirectory(prefix="taloscluster-metal-verify-") as tmp:
            talosconfig = Path(tmp) / "talosconfig"
            talosconfig.write_text(talosctl.gen_talosconfig(cfg.name, ip, secrets))
            _wait_configured(server, ip, talosconfig, timeout_s, interval_s)
        return
    _wait_configured(server, ip, talosconfig, timeout_s, interval_s)


def _wait_configured(server: MetalServer, ip: str, talosconfig: Path,
                     timeout_s: int, interval_s: int) -> None:
    info(
        f"waiting for {server.name} to come back with its configuration "
        f"(up to {timeout_s // 60}m)..."
    )
    deadline = time.monotonic() + timeout_s
    while True:
        # maintenance mode answers without credentials; once the config is
        # applied the node reboots into it and only the cluster apid answers
        if (
            not talosctl.maintenance_reachable(ip)
            and talosctl.reachable(talosconfig, endpoint=ip, node=ip)
        ):
            version = talosctl.server_version(talosconfig, ip, ip)
            info(f"{server.name} is up, running {version or 'apid'}")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{server.name} did not come back with its configuration within "
                f"{timeout_s // 60}m; check the machine's console -- the install "
                "may have failed"
            )
        time.sleep(interval_s)


def join(root: Path, name: str, *, serve: bool = False) -> None:
    """The whole flow: boot, wait, apply, eject, verify.

    A machine whose redfish is off is never touched through its BMC: the
    flow becomes wait, apply, verify, for a machine the operator booted
    into maintenance mode by other means.
    """
    cfg = load_config(root)
    server = _find_server(cfg, name)
    _refuse_joined(root, server)
    if server.redfish:
        boot(root, name, serve=serve, foreground=False)
    else:
        _no_bmc(server)
    wait(root, name)
    apply(root, name)
    if server.redfish:
        eject(root, name)
    verify(root, name)


def _refuse_joined(root: Path, server: MetalServer) -> None:
    """Refuse a machine that already runs this cluster's configuration.

    boot, apply and join would drive it back through the install media,
    which wipes an installed node -- a control plane's etcd with it.
    """
    talosconfig = root / "talosconfig"
    if not talosconfig.is_file():
        return
    ip = _cluster_ip(server)
    if not talosctl.maintenance_reachable(ip) and talosctl.reachable(
        talosconfig, endpoint=ip, node=ip
    ):
        raise ReconcileError(
            f"metal server {server.name} already answers apid with this cluster's "
            f"identity on {ip}; reinstalling it from the install media would wipe "
            "the machine -- run `taloscluster converge` for a config change, or "
            "reset the machine first if a re-join is really intended"
        )


# -- LAN install media ---------------------------------------------------------


def _local_address_for(target: str) -> str:
    """This machine's address on the network that reaches `target`.

    A UDP connect sends no packets but asks the routing table which source
    address traffic to `target` would carry -- the address the BMC must fetch
    the ISO from.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        return str(sock.getsockname()[0])
    except OSError as e:
        raise ReconcileError(
            f"no local address routes to the BMC at {target}: {e}"
        ) from e
    finally:
        sock.close()


def _download_iso(url: str, dest_dir: Path) -> Path:
    dest = dest_dir / url.rsplit("/", 1)[-1]
    action(f"download {url} -> {dest}")
    try:
        with requests.get(url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
    except requests.RequestException as e:
        raise ReconcileError(f"could not download {url}: {e}") from e
    return dest


class _ServedIso:
    """The downloaded ISO plus the LAN server handing it out.

    The scratch copy and the server live until the process exits (the media is
    ejected long before that), which is what `join --serve` relies on while the
    machine boots, waits and applies.
    """

    def __init__(self, url: str):
        self.dir = Path(tempfile.mkdtemp(prefix="taloscluster-metal-iso-"))
        atexit.register(shutil.rmtree, self.dir, True)
        self.iso = _download_iso(url, self.dir)
        self.server = _IsoServer(self.iso)

    def url_for(self, host: str) -> str:
        return self.server.url_for(host)


class _IsoServer:
    """Serve one ISO file over HTTP for BMCs on the LAN.

    BMCs fetch a URL-mounted image lazily and sometimes with Range requests,
    so a threaded server hands the file out for as long as the process runs.
    """

    def __init__(self, iso: Path):
        self.iso = iso
        size = iso.stat().st_size
        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), _iso_handler(iso, size))
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True, name="metal-iso",
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def url_for(self, host: str) -> str:
        return f"http://{host}:{self.port}/{self.iso.name}"


def _iso_handler(iso: Path, size: int) -> type[BaseHTTPRequestHandler]:
    """An HTTP handler class serving `iso`, single-range requests included."""

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()

        def do_GET(self) -> None:
            start, end, status = self._range()
            if status == 416:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            try:
                with iso.open("rb") as fh:
                    fh.seek(start)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(length))
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the BMC hung up mid-transfer; there is nothing to answer

        def _range(self) -> tuple[int, int, int]:
            header = (self.headers.get("Range") or "").strip()
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not (match.group(1) or match.group(2)):
                return 0, size - 1, 200
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                # a suffix range: the final N bytes
                start = max(size - int(last), 0)
                end = size - 1
            if start >= size:
                return 0, size - 1, 416
            return start, end, 206

        def log_message(self, fmt: str, *args: object) -> None:
            info(f"iso server: {self.address_string()} {fmt % args}")

    return Handler
