"""`taloscluster init`: scaffold a new cluster directory.

Writes the two files a cluster needs before the first converge — cluster.yaml
(desired state, committable) and secrets.yaml (credentials, gitignored, 0600) —
plus a .gitignore that keeps the secret/derived files out of git. Existing
cluster.yaml / secrets.yaml keep their content and receive only missing sections
from installed plugins; an existing .gitignore is appended to only with entries
it is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import plugins as _plugins
from .config import CLUSTER_FILE, SECRETS_FILE, read_yaml
from .output import info, log
from .state import DERIVED_FILES
from .state import SECRETS_FILE as TALOS_SECRETS_FILE

CLUSTER_TEMPLATE = """\
# Cluster definition — desired state, safe to commit (note: the `security`
# allowlists reveal which source addresses may reach your apis).
# Edit and run `taloscluster plan` / `taloscluster converge`.
name: {name}

# extra tags exposed by talos as kubernetes node labels (machine.nodeLabels);
# the provider project/pool name is always added as ncsa/project (spaces -> _).
# per-pool tags: are also supported and override these on key collision.
# tags:
#   team: platform

talos:
  version: v1.13.8
  # extensions: []       # extra cluster-wide extensions (base set is always baked in)
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
  cidr: 192.168.0.0/21
  dns: [8.8.8.8, 8.8.4.4]
  ntp: [pool.ntp.org]

# source CIDRs allowed to reach the kube api (6443) and talos api (50000);
# friendly name -> CIDR
security:
  kubernetes:
    # office vpn: 203.0.113.0/24
    tailscale: 100.64.0.0/10
  talos:
    # office vpn: 203.0.113.0/24
    tailscale: 100.64.0.0/10

tailscale:
  login_server: https://headscale.example.edu
"""

SECRETS_TEMPLATE = """\
# Secrets for this cluster — never commit (gitignored by `taloscluster init`).
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
      kubeapi_vip: 192.168.0.10""",
        "secrets": """\
proxmox:
  token_id: "taloscluster@pve!provider"
  token_secret: "CHANGE-ME"
""",
    },
}

# everything a cluster directory produces that must never reach git
GITIGNORE_ENTRIES = (
    SECRETS_FILE,             # secrets.yaml
    TALOS_SECRETS_FILE,       # talossecrets.yaml
    *DERIVED_FILES,           # talosconfig, kubeconfig
)


def init(root: Path, name: str, provider: str = "openstack") -> None:
    """Create provider-specific cluster.yaml and secrets.yaml plus .gitignore."""
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
            provider_section=template["cluster"],
        ))
        info(f"wrote {CLUSTER_FILE}")

    secrets = root / SECRETS_FILE
    if secrets.exists():
        info(f"{SECRETS_FILE} exists, keeping existing content")
    else:
        secrets.write_text(SECRETS_TEMPLATE.format(
            provider_section=template["secrets"],
        ))
        os.chmod(secrets, 0o600)
        info(f"wrote {SECRETS_FILE} (mode 0600)")

    _plugins.initialize(root)
    _ensure_gitignore(root)

    log("next steps")
    credential = (
        "openstack application credential"
        if provider == "openstack"
        else "proxmox api token"
    )
    info(f"1. edit {SECRETS_FILE}: {credential} + tailscale key")
    info(f"2. edit {CLUSTER_FILE}: name, versions, pools, {provider} settings, allowlists")
    info("3. taloscluster plan      # dry-run, changes nothing")
    info("4. taloscluster converge  # create the cluster")


def add_yaml_section(path: Path, key: str, section: str) -> None:
    """Append a plugin section when its top-level key is not already present."""
    if key in read_yaml(path):
        info(f"{path.name}: {key} section already exists")
        return

    current = path.read_text()
    if current and not current.endswith("\n"):
        separator = "\n\n"
    elif current and not current.endswith("\n\n"):
        separator = "\n"
    else:
        separator = ""
    with path.open("a") as f:
        f.write(separator)
        f.write(section.rstrip() + "\n")
    info(f"{path.name}: added {key} section")


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
