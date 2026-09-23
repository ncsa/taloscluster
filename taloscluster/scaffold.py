"""`taloscluster init`: scaffold a new cluster directory.

Writes the two files a cluster needs before the first converge — cluster.yaml
(desired state, committable; it lists secrets.yaml under `include:`) and
secrets.yaml (credentials, gitignored, 0600) — plus a .gitignore that keeps the
secret/derived files out of git. Existing cluster.yaml / secrets.yaml keep their
content and receive only missing sections from installed plugins (and, with
--metal, the bare-metal section plus the KubeSpan opt-in its off-L2 example
group needs); an existing .gitignore is appended to only with entries it is
missing.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import plugins as _plugins
from .config import CLUSTER_FILE, SECRETS_FILE, read_yaml
from .output import Die, info, log
from .state import DERIVED_FILES, write_private
from .state import SECRETS_FILE as TALOS_SECRETS_FILE

CLUSTER_TEMPLATE = """\
# Cluster definition — desired state, safe to commit (note: the `security`
# allowlists reveal which source addresses may reach your apis).
# Edit and run `taloscluster plan` / `taloscluster converge`.
name: {name}

# secrets.yaml (gitignored, mode 0600) is merged in through this include;
# keep the line or the loader refuses to merge the credentials
include: [secrets.yaml]

# extra tags exposed by talos as kubernetes node labels (machine.nodeLabels);
# every node also gets ncsa/role and ncsa/pool; OpenStack adds ncsa/project
# when the project name is available (spaces -> _).
# per-pool tags: are also supported and override these on key collision.
# tags:
#   team: platform

talos:
  version: v1.13.8
{kubespan}  # extensions: []       # extra cluster-wide extensions (base set is always baked in)
  # config_patches: []   # freeform machine-config YAML applied to all nodes
kubernetes:
  version: v1.36.1

controlplane:
  count: 3 # keep odd; 1 works (no HA), 3+ recommended
{controlplane_sizing}
  disk: 40 # GB, boot volume

# worker pools; add a pool (e.g. gpu) or bump a count and re-run converge
workers:
  worker:
    count: 3
{worker_sizing}
    disk: 100

{provider_section}

network:
  cluster: # the L2 the nodes sit on
    cidr: 192.168.0.0/21{network_cluster}
{dns}
  ntp: [pool.ntp.org]

# named ingress rules: friendly name -> CIDR allowed to reach that rule's port.
# kubernetes defaults to 6443 and talos to 50000; any other rule needs a `port`.
# tcp/80 and tcp/443 stay open to everyone until some rule claims that port
# (an `http:` or `https:` rule, or any other rule pointed at 80 or 443).
# Claiming a port with an empty hosts map closes it to everyone.
security:
  kubernetes:
    # office vpn: 203.0.113.0/24
    tailscale: 100.64.0.0/10
  talos:
    # office vpn: 203.0.113.0/24
    tailscale: 100.64.0.0/10
  # https:                        # restrict tcp/443 (omit to leave it open)
  #   hosts:
  #     office vpn: 203.0.113.0/24
  # metrics:                      # any other port needs an explicit `port`
  #   port: 9100
  #   hosts:
  #     office vpn: 203.0.113.0/24

tailscale:
  login_server: https://headscale.example.edu
"""

SECRETS_TEMPLATE = """\
# Secrets for this cluster — never commit (gitignored by `taloscluster init`).
# cluster.yaml lists this file under `include:`, so these are ordinary
# cluster.yaml keys that happen to live here; you may move them to any file
# `include:` names, or into cluster.yaml itself.
{provider_section}
tailscale:
  # reusable (ideally ephemeral) pre-auth key so all nodes can register;
  # omit to leave the baked-in tailscale extension idle
  auth_key: "CHANGE-ME"
"""

PROVIDER_TEMPLATES = {
    "openstack": {
        "controlplane_sizing": "  flavor: gp.medium",
        "worker_sizing": "    flavor: gp.xlarge",
        "network_cluster": "",
        "dns": "  dns: [8.8.8.8, 8.8.4.4]",
        "cluster": """\
openstack:
  url: https://openstack.example.edu:5000/v3/
  availability_zone: nova
  external_net: ext-net""",
        "secrets": """\
openstack:
  # application credential: openstack application credential create taloscluster
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
""",
    },
    "proxmox": {
        "controlplane_sizing": "  cores: 4\n  memory: 8 # GB",
        "worker_sizing": "    cores: 8\n    memory: 16 # GB",
        "network_cluster": """
    # the VIP must sit OUTSIDE any DHCP range on this network and, with a
    # managed SDN, outside the static layout (.2 is the first free host: the
    # layout reserves .1 for the gateway, the controlplane block, and a
    # block per worker pool)
    kubeapi_vip: 192.168.0.2""",
        # a bridge/vnet serves DNS over DHCP, so resolvers here would only earn
        # the ignored-network.dns warning on the first plan; a managed SDN has
        # no DHCP and needs them filled in
        "dns": """
  # DNS comes from DHCP on a bridge/vnet; a managed SDN has no DHCP and
  # needs the resolvers set here
  dns: []""",
        "cluster": """\
proxmox:
  url: https://pve.example.edu:8006
  storage: local-lvm
  iso_storage: local
  cidata_storage: local # node-local; temporarily contains machine secrets
  placement_strategy: spread
  # nodes: [pve1, pve2, pve3] # omit to discover all online nodes
  network:
    cluster:
      bridge: vmbr0 # use vnet instead for an existing Proxmox SDN VNet
      # sdn: {} # or replace bridge with a managed EVPN SDN network; all fields
      #         # optional (name, zone, controller, asn, vrf_tag, tag, exit_nodes,
      #         # primary_exit_node, mtu, nodes); the zone/VNet id is `name`
      #         # (default: the cluster name, max 8 chars, no hyphens); removing
      #         # a set mtu/nodes later does not unset it on the zone""",
        "secrets": """\
proxmox:
  token_id: "taloscluster@pve!provider"
  token_secret: "CHANGE-ME"
""",
    },
}

# `init --metal` scaffolds its example group on another L2, and the KubeSpan
# overlay is what carries that group's traffic to the cluster -- so the
# scaffolded talos section opts in. A plain scaffold omits the key: KubeSpan
# is off by default.
METAL_KUBESPAN_SECTION = """\
  # the bare-metal example group below sits on another L2, so the KubeSpan
  # overlay is on; a single-L2 cluster omits this key
  kubespan: true
"""

# the bare-metal section `init --metal` appends: one example group with one
# server. `redfish` starts false so the scaffolded pair loads with the
# placeholder BMC credentials still in secrets.yaml.
METAL_CLUSTER_SECTION = """\
# bare-metal machines, joined with `taloscluster metal join`; each group is the
# defaults its servers start from, and each server overrides its own
metal:
  rack1:
    role: worker
    redfish: false # set true once the BMC credentials in secrets.yaml are real
    disk: /dev/sda
    network: # optional; omit to sit on network.cluster
      cidr: 192.168.8.0/24
      gateway: 192.168.8.1
    interfaces:
      enp1s0f0: { role: pxe }
      enp2s0f0: { role: cluster }
    servers:
      srv01:
        bmc: { ip: 192.168.8.51 }
        interfaces:
          enp2s0f0: { ip: 192.168.8.11/24 }
"""

METAL_SECRETS_SECTION = """\
# bare-metal BMC credentials, per group; real values are required before a
# group's `redfish: true` will load
metal:
  rack1:
    bmc:
      username: "CHANGE-ME"
      password: "CHANGE-ME"
"""

# everything a cluster directory produces that must never reach git
GITIGNORE_ENTRIES = (
    SECRETS_FILE,             # secrets.yaml
    TALOS_SECRETS_FILE,       # talossecrets.yaml
    *DERIVED_FILES,           # talosconfig, kubeconfig
    ".metal/",                # generated metal machine configs (cluster credentials)
)


def init(
    root: Path, name: str, provider: str | None = "openstack", metal: bool = False
) -> None:
    """Create provider-specific cluster.yaml and secrets.yaml plus .gitignore.

    `metal` appends the bare-metal section templates beside the provider; it
    is refused without one, since bare metal joins a provider-managed cluster.
    """
    if provider is None and not metal:
        provider = "openstack"
    if provider is None:
        raise Die(
            "--metal requires a VM provider: bare-metal machines join a cluster "
            "a provider manages, so pass --openstack or --proxmox"
        )
    try:
        template = PROVIDER_TEMPLATES[provider]
    except KeyError as e:
        raise ValueError(f"unsupported provider: {provider}") from e

    root.mkdir(parents=True, exist_ok=True)

    log(f"init {root}")

    cluster = root / CLUSTER_FILE
    if cluster.exists():
        info(f"{CLUSTER_FILE} exists, keeping existing content")
    else:
        cluster.write_text(CLUSTER_TEMPLATE.format(
            name=name,
            controlplane_sizing=template["controlplane_sizing"],
            worker_sizing=template["worker_sizing"],
            network_cluster=template["network_cluster"],
            dns=template["dns"],
            provider_section=template["cluster"],
            kubespan=METAL_KUBESPAN_SECTION if metal else "",
        ))
        info(f"wrote {CLUSTER_FILE}")

    secrets = root / SECRETS_FILE
    if secrets.exists():
        info(f"{SECRETS_FILE} exists, keeping existing content")
    else:
        write_private(secrets, SECRETS_TEMPLATE.format(
            provider_section=template["secrets"],
        ))
        info(f"wrote {SECRETS_FILE} (mode 0600)")

    if metal:
        _add_kubespan(cluster)
        add_yaml_section(cluster, "metal", METAL_CLUSTER_SECTION)
        add_yaml_section(secrets, "metal", METAL_SECRETS_SECTION)

    _plugins.initialize(root)
    _ensure_gitignore(root)

    log("next steps")
    credentials = []
    if provider == "openstack":
        credentials.append("openstack application credential")
    elif provider == "proxmox":
        credentials.append("proxmox api token")
    if metal:
        credentials.append("metal BMC credentials")
    credentials.append("tailscale key")
    info(f"1. edit {SECRETS_FILE}: {' + '.join(credentials)}")
    info(f"2. edit {CLUSTER_FILE}: name, versions, pools, {provider} settings, allowlists")
    if metal:
        info("   (a long machine list can move into a file `include:` names)")
    info("3. taloscluster plan      # dry-run, changes nothing")
    info("4. taloscluster converge  # create the cluster")


def add_yaml_section(path: Path, key: str, section: str) -> None:
    """Append a plugin section when its top-level key is not already present.

    A commented top-level `key:` line counts as present too: a secrets
    scaffold can be comments only (a comment opts nothing in), so its
    `# key:` header is what a re-run must recognise.
    """
    text = path.read_text()
    if key in read_yaml(path) or any(
        line.startswith(f"{key}:")
        or (line.startswith("#") and line.lstrip("#").strip().startswith(f"{key}:"))
        for line in text.splitlines()
    ):
        info(f"{path.name}: {key} section already exists")
        return

    if text and not text.endswith("\n"):
        separator = "\n\n"
    elif text and not text.endswith("\n\n"):
        separator = "\n"
    else:
        separator = ""
    with path.open("a") as f:
        f.write(separator)
        f.write(section.rstrip() + "\n")
    info(f"{path.name}: added {key} section")


def _add_kubespan(path: Path) -> None:
    """Opt an existing cluster.yaml's talos section into KubeSpan.

    `init --metal` appends its example group on another L2 to an existing
    directory too, and that group only loads with the overlay on; a fresh
    scaffold writes the opt-in through its template instead. A key already
    present is left alone -- an explicit false is the user's choice, and the
    load refuses an off-L2 group under it by name.
    """
    talos = read_yaml(path).get("talos")
    if isinstance(talos, dict) and talos.get("kubespan") is not None:
        return
    lines = path.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.fullmatch(r"talos:\s*(#.*)?", line.rstrip()):
            lines.insert(i + 1, METAL_KUBESPAN_SECTION)
            path.write_text("".join(lines))
            info(f"{path.name}: enabled talos.kubespan")
            return


def _ensure_gitignore(root: Path) -> None:
    """Create .gitignore, or append only the entries an existing one lacks."""
    path = root / ".gitignore"
    if not path.exists():
        path.write_text("".join(f"{e}\n" for e in GITIGNORE_ENTRIES))
        info("wrote .gitignore")
        return

    present = {line.strip() for line in path.read_text().splitlines()}
    missing = [e for e in GITIGNORE_ENTRIES if e not in present]
    if not missing:
        info(".gitignore already covers the secret/derived files")
        return
    with path.open("a") as f:
        if present and not path.read_text().endswith("\n"):
            f.write("\n")
        f.write("# added by taloscluster init\n")
        f.writelines(f"{e}\n" for e in missing)
    info(f".gitignore: added {', '.join(missing)}")
