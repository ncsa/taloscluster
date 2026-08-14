"""`clusterctl init`: scaffold a new cluster directory.

Writes the two files a cluster needs before the first converge — cluster.yaml
(desired state, committable) and secrets.yaml (credentials, gitignored, 0600) —
plus a .gitignore that keeps the secret/derived files out of git. Existing
cluster.yaml / secrets.yaml are never touched; an existing .gitignore is
appended to only with entries it is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import CLUSTER_FILE, SECRETS_FILE
from .output import info, log
from .state import DERIVED_FILES
from .state import SECRETS_FILE as TALOS_SECRETS_FILE

CLUSTER_TEMPLATE = """\
# Cluster definition — desired state, safe to commit (note: the `security`
# allowlists reveal which source addresses may reach your apis).
# Edit and run `clusterctl plan` / `clusterctl converge`.
name: {name}

talos:
  version: v1.13.8
  # extensions: []       # extra cluster-wide extensions (base set is always baked in)
  # config_patches: []   # freeform machine-config YAML applied to all nodes
kubernetes:
  version: v1.36.1

controlplane:
  count: 3 # keep odd; 1 works (no HA), 3+ recommended
  flavor: gp.medium
  disk: 40 # GB, boot volume

# worker pools; add a pool (e.g. gpu) or bump a count and re-run converge
workers:
  worker:
    count: 3
    flavor: gp.xlarge
    disk: 100

openstack:
  url: https://openstack.example.edu:5000/v3/
  availability_zone: nova
  external_net: ext-net

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
# Secrets for this cluster — never commit (gitignored by `clusterctl init`).
openstack:
  # application credential: openstack application credential create clusterctl
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
tailscale:
  # reusable (ideally ephemeral) pre-auth key so all nodes can register;
  # omit to leave the baked-in tailscale extension idle
  auth_key: "CHANGE-ME"
"""

# everything a cluster directory produces that must never reach git
GITIGNORE_ENTRIES = (
    SECRETS_FILE,             # secrets.yaml
    TALOS_SECRETS_FILE,       # talossecrets.yaml
    *DERIVED_FILES,           # talosconfig, kubeconfig
)


def init(root: Path, name: str) -> None:
    """Create cluster.yaml, secrets.yaml and .gitignore in `root`."""
    root.mkdir(parents=True, exist_ok=True)

    log(f"init {root}")

    cluster = root / CLUSTER_FILE
    if cluster.exists():
        info(f"{CLUSTER_FILE} exists, leaving it alone")
    else:
        cluster.write_text(CLUSTER_TEMPLATE.format(name=name))
        info(f"wrote {CLUSTER_FILE}")

    secrets = root / SECRETS_FILE
    if secrets.exists():
        info(f"{SECRETS_FILE} exists, leaving it alone")
    else:
        secrets.write_text(SECRETS_TEMPLATE)
        os.chmod(secrets, 0o600)
        info(f"wrote {SECRETS_FILE} (mode 0600)")

    _ensure_gitignore(root)

    log("next steps")
    info(f"1. edit {SECRETS_FILE}: openstack application credential + tailscale key")
    info(f"2. edit {CLUSTER_FILE}: name, versions, pools, openstack endpoint, allowlists")
    info("3. clusterctl plan      # dry-run, changes nothing")
    info("4. clusterctl converge  # create the cluster")


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
        f.write("# added by clusterctl init\n")
        f.writelines(f"{e}\n" for e in missing)
    info(f".gitignore: added {', '.join(missing)}")
