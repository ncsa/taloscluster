"""Reconcile a cluster's registration + members in Rancher.

`converge` makes Rancher match the `rancher:` section of cluster.yaml:
  1. the cluster is imported into Rancher (or reused if the existing Rancher
     cluster id matches the downstream cluster's cattle-cluster-agent), and
  2. the cattle-cluster-agent is installed into the downstream cluster via
     kubectl (using the local kubeconfig) so Rancher marks it Active, and
  3. every admin/user (by netid) is added with the right role
     (admins -> cluster-owner, users -> cluster-member), and members no longer in
     the config have their cluster membership (binding) removed. The cluster
     creator/owner is always preserved.

Name collisions: if a Rancher cluster already bears the configured name but the
downstream cluster has no Rancher agent (or a different id), we refuse to attach
to it and abort — it is an unrelated cluster with the same name. Safe to re-run:
an existing cluster matching the downstream agent id is reused, and member
reconciliation is idempotent.

`destroy` removes every member that this config declares (admins + users). It
does NOT delete the cluster from Rancher. The owner (the user behind the token)
is preserved because only bindings matching the configured netids are removed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from taloscluster.context import Context
from taloscluster.output import action, dry_run, info, log, warn

from .client import Client
from .config import ROLE_BY_TIER, Config, Secrets
from .errors import RancherError

# Tiers are reconciled in this order so destroy/cleanup can rely on the list.
TIERS = ("admins", "users")


def _load(root: Path):
    cfg = Config.load(root)
    secrets = Config.load_secrets(root)
    return cfg, secrets


def _client(secrets: Secrets) -> Client:
    return Client(secrets.rancher_url, secrets.rancher_token)


def _kubectl(root: Path, *args: str) -> str | None:
    """Run kubectl against the cluster's kubeconfig; None on failure."""
    proc = subprocess.run(
        ["kubectl", "--kubeconfig", str(root / "kubeconfig"), *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def downstream_rancher_id(root: Path) -> str | None:
    """The Rancher cluster id (c-xxxxx) the downstream cluster is registered as.

    Read from the cattle-cluster-agent credentials secret in cattle-system
    (`cattle-credentials-*` secret, `namespace` key). Returns None when Rancher is
    not installed (no agent), meaning the cluster is unrelated to any existing
    Rancher cluster and can't be a re-run.
    """
    if _kubectl(root, "get", "ns", "cattle-system") is None:
        return None
    import base64
    import json
    secrets = _kubectl(root, "get", "secret", "-n", "cattle-system", "-o", "json")
    if not secrets:
        return None
    try:
        doc = json.loads(secrets)
    except ValueError:
        return None
    for item in doc.get("items", []):
        if item.get("metadata", {}).get("name", "").startswith("cattle-credentials"):
            ns = item.get("data", {}).get("namespace")
            if ns:
                try:
                    return base64.b64decode(ns).decode()
                except Exception:
                    pass
    return None


def ensure_members(client: Client, cid: str, cfg: Config) -> list[dict[str, str]]:
    """Reconcile cluster members to the configured admins/users.

    Adds any declared member missing the right role, and removes any existing
    member who is no longer in the config (so taking a user out of the admin/user
    list deletes their cluster membership). The cluster creator/owner -- the
    `<cluster>:creator-cluster-owner` binding -- is always preserved.
    """
    owner_binding = f"{cid}:creator-cluster-owner"
    desired_by_role: dict[str, set[str]] = {role: set() for role in ROLE_BY_TIER.values()}
    display: dict[str, tuple[str, str]] = {}  # pid -> (netid, tier) for messages

    for tier in TIERS:
        role = ROLE_BY_TIER[tier]
        for netid in cfg.members.netids_for(tier):
            principal = client.resolve_principal(netid)
            if principal is None:
                warn(f"could not resolve principal for {netid!r} (tier {tier}); skipping")
                continue
            pid = principal["id"]
            desired_by_role[role].add(pid)
            display[pid] = (netid, tier)

    bindings = client.list_member_bindings(cid)
    current = _by_principal_role(bindings)

    # add missing / correct
    desired_role_for: dict[str, str] = {}
    for role, pids in desired_by_role.items():
        for pid in pids:
            desired_role_for[pid] = role
            if (pid, role) in current:
                netid, _ = display[pid]
                info(f"{netid} ({pid}) already {role}")
            else:
                client.add_member(cid, pid, role)
                current.add((pid, role))

    # remove stale members (not desired, not owner) or fix wrong-role bindings
    for b in bindings:
        if not b.userPrincipalId or b.groupPrincipalId:
            continue  # group binding; don't touch
        if b.id == owner_binding:
            continue  # creator/owner binding; always preserve
        if b.userPrincipalId not in desired_role_for:
            info(f"removing stale member binding {b.userPrincipalId} ({b.roleTemplateId})")
            client.remove_member(b.id)
        elif desired_role_for[b.userPrincipalId] != b.roleTemplateId:
            info(
                f"correcting member role for {b.userPrincipalId} "
                f"({b.roleTemplateId} -> {desired_role_for[b.userPrincipalId]})"
            )
            client.remove_member(b.id)

    return [
        {"netid": display[pid][0], "role": role}
        for pid, role in sorted(desired_role_for.items())
    ]


def _by_principal_role(bindings) -> set[tuple[str, str]]:
    """Index bindings by (principal, role); drop group bindings (untyped)."""
    return {
        (b.userPrincipalId, b.roleTemplateId)
        for b in bindings
        if b.userPrincipalId and not b.groupPrincipalId
    }


def install_agent(root: Path, client: Client, cluster, agent_installed: bool = False) -> None:
    """Install cattle-cluster-agent into the downstream cluster via kubectl.

    Uses the gitignored kubeconfig in the cluster dir. Runs after a fresh
    registration so Rancher flips the cluster to Active. A no-op in dry-run or
    when the agent is already installed on the downstream cluster (idempotent
    re-run) -- never re-applies on re-run.
    """
    if cluster.id == "<dry-run>" or agent_installed:
        return
    command = client.fetch_import_command(cluster)
    if not command:
        warn("no import command returned by Rancher; skipping agent install")
        return
    if dry_run():
        kc = root / "kubeconfig"
        action(f"apply Rancher import manifest via kubectl (--kubeconfig {kc})")
        return
    action("installing cattle-cluster-agent into the cluster via kubectl")
    try:
        subprocess.run(
            ["kubectl", "--kubeconfig", str(root / "kubeconfig"), "apply", "-f", "-"],
            input=command, text=True, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RancherError(f"kubectl apply of import manifest failed: {e.stderr.strip()}") from e


def converge(ctx: Context, assume_yes: bool = False) -> dict:
    """Register the cluster and reconcile its members.

    The returned dict lands in ``ctx.results["rancher"]``, so a plugin that runs
    after this one (argocd declares ``AFTER = ("rancher",)``) can pick up the
    Rancher cluster id without asking Rancher itself.
    """
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)

    downstream_id = downstream_rancher_id(ctx.root)

    log("ensure cluster is registered in Rancher")
    cluster = client.ensure_cluster(cfg.name, downstream_id=downstream_id)

    log("ensure rancher agent is installed (skipped if already registered)")
    install_agent(ctx.root, client, cluster, agent_installed=downstream_id is not None)

    log("reconcile cluster members")
    members = ensure_members(client, cluster.id, cfg)
    info("done")
    return {"cluster_id": cluster.id, "url": secrets.rancher_url, "members": members}


def _remove_agent(root: Path) -> None:
    """Remove the Rancher agent (cattle-system) from the downstream cluster."""
    if _kubectl(root, "get", "ns", "cattle-system") is None:
        return
    if dry_run():
        action("delete cattle-system namespace (uninstall Rancher agent) via kubectl")
        return
    action("uninstalling Rancher agent: delete cattle-system via kubectl")
    _kubectl(root, "delete", "ns", "cattle-system")


def destroy(ctx: Context, assume_yes: bool = False) -> None:
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)

    cluster = client.find_cluster(cfg.name)
    if cluster is None:
        info(f"cluster {cfg.name} not registered in Rancher; nothing to remove")
        return

    log("delete cluster from Rancher")
    client.delete_cluster(cluster.id)

    log("uninstall Rancher agent from the downstream cluster")
    _remove_agent(ctx.root)
    info("done")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _desired_members(client: Client, cfg: Config) -> dict[str, str]:
    """principal id -> role for every member cluster.yaml declares.

    Unresolvable netids are skipped (they are reported by converge/check), so a
    typo shows up as a missing member rather than an exception.
    """
    out: dict[str, str] = {}
    for tier in TIERS:
        for netid in cfg.members.netids_for(tier):
            principal = client.resolve_principal(netid)
            if principal is not None:
                out[principal["id"]] = ROLE_BY_TIER[tier]
    return out


def status(ctx: Context) -> dict:
    """What Rancher currently knows about this cluster."""
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)
    cluster = client.find_cluster(cfg.name)
    if cluster is None:
        return {"registered": False, "url": secrets.rancher_url}
    return {
        "registered": True,
        "url": secrets.rancher_url,
        "cluster_id": cluster.id,
        "agent_installed": downstream_rancher_id(ctx.root) is not None,
        "members": sorted(
            f"{b.userPrincipalId} ({b.roleTemplateId})"
            for b in client.list_member_bindings(cluster.id)
            if b.userPrincipalId and not b.groupPrincipalId
        ),
    }


def check(ctx: Context) -> dict:
    """Would a converge change anything in Rancher?

    Not ok when the cluster is unregistered, the downstream agent is missing, or
    the actual bindings differ from the desired ones -- the same three things
    converge fixes.
    """
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)
    cluster = client.find_cluster(cfg.name)
    if cluster is None:
        return {"ok": False, "registered": False,
                "reason": f"cluster {cfg.name} is not registered in Rancher"}

    agent = downstream_rancher_id(ctx.root) is not None
    desired = {(pid, role) for pid, role in _desired_members(client, cfg).items()}
    actual = _by_principal_role(client.list_member_bindings(cluster.id))
    owner = f"{cluster.id}:creator-cluster-owner"
    # the creator binding is never reconciled away, so it is not "extra"
    extra = {
        (b.userPrincipalId, b.roleTemplateId)
        for b in client.list_member_bindings(cluster.id)
        if b.userPrincipalId and not b.groupPrincipalId and b.id != owner
    } - desired

    missing = desired - actual
    return {
        "ok": agent and not missing and not extra,
        "registered": True,
        "cluster_id": cluster.id,
        "agent_installed": agent,
        "missing_members": sorted(f"{p} ({r})" for p, r in missing),
        "stale_members": sorted(f"{p} ({r})" for p, r in extra),
    }
