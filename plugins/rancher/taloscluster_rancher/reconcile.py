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
to it and abort — it is an unrelated cluster with the same name. The mirror case
is refused too: when the downstream agent is registered (a non-None id) but no
Rancher cluster carries the configured name — the registration was renamed or
deleted in the Rancher UI — a fresh import would strand the agent
under the old id, so we refuse rather than create an inert cluster that can never
match the agent. That agent is orphaned (its id matches no Rancher cluster), and
deleting the stale registration in Rancher will not clear it; `destroy` uninstalls
the orphaned agent from the downstream cluster so a later converge can register
fresh. Safe to re-run: an existing cluster matching the downstream agent
id is reused, and member reconciliation is idempotent.

`destroy` deletes the cluster from Rancher and uninstalls the Rancher agent
from the downstream cluster. Like `converge`, it refuses to touch a Rancher
cluster whose id does not match the downstream cluster's cattle-cluster-agent,
so an unrelated cluster bearing the same name is never deleted. When the
downstream agent is orphaned -- its id matches no Rancher cluster bearing the
configured name (the registration was renamed or deleted in the Rancher UI) --
`destroy` uninstalls the orphaned agent from the downstream cluster instead of
claiming there is nothing to remove.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from taloscluster.context import Context
from taloscluster.errors import ConfigError
from taloscluster.k8s import kubectl, rancher
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
    """Run kubectl against the cluster's kubeconfig; None on failure.

    A kubectl request that times out (the kube-api accepted TCP but never
    answered -- not merely returning non-zero) raises RancherError, so a hung
    api is not mistaken for "cattle-system is absent".
    """
    try:
        proc = kubectl._run(
            [kubectl.BIN, "--kubeconfig", str(root / "kubeconfig"), *args],
            capture=True, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RancherError(
            f"{kubectl.display([kubectl.BIN, '--kubeconfig', str(root / 'kubeconfig'), *args])} "
            f"against the downstream cluster timed out ({kubectl.RUN_TIMEOUT:.0f}s); "
            f"investigate the cluster's kube-api and retry"
        ) from e
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def downstream_rancher_id(root: Path) -> str | None:
    """The Rancher cluster id (c-xxxxx) the downstream cluster is registered as.

    Read from the cattle-cluster-agent credentials secret in cattle-system
    (`cattle-credentials-*` secret, `namespace` key) via the shared
    `taloscluster.k8s.rancher` helper. Returns None only when the cluster has no
    Rancher identity to act on (no agent, or an agent still installing); a read
    that fails while cattle-system exists raises, so a transient kube-api error
    is never mistaken for "no agent" and never re-registers the cluster.
    """
    return rancher.cluster_id(root / "kubeconfig")


def _resolve_members(client: Client, cfg: Config) -> dict[str, tuple[str, str]]:
    """principal id -> (netid, tier) for every configured member that resolves.

    Resolves the complete desired set before returning, so a caller can never act
    on a partially-resolved set. Raises ConfigError when two distinct configured
    netids from different tiers resolve to the same principal (e.g. `alice` and
    `alice@example.com`, whose email suffix resolve_principal strips), because
    that membership is ambiguous and would flap between cluster-owner and
    cluster-member on alternating runs. Raises RancherError when a configured
    member cannot be resolved at all: skipping it let reconcile treat that user's
    existing binding as stale and delete it, and let check report `ok: true`
    while the configured admin was still unresolved. Refusing up front is what
    stops an existing binding from being removed as stale and a never-created
    member from being silently ignored.
    """
    by_pid: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for tier in TIERS:
        for netid in cfg.members.netids_for(tier):
            principal = client.resolve_principal(netid)
            if principal is None:
                unresolved.append(f"{netid!r} (tier {tier})")
                continue
            pid = principal["id"]
            existing = by_pid.get(pid)
            if existing:
                other_netid, other_tier = existing
                raise ConfigError(
                    f"{netid!r} and {other_netid!r} both resolve to the same Rancher "
                    f"principal ({pid}) but sit in different tiers ('{tier}' and "
                    f"'{other_tier}'); the membership would flap between roles, so "
                    "use canonical netids for both"
                )
            by_pid[pid] = (netid, tier)
    if unresolved:
        raise RancherError(
            "could not resolve Rancher principals for configured member(s): "
            + ", ".join(unresolved)
            + "; refusing to change memberships so an existing binding is not "
            "removed as stale"
        )
    return by_pid


def ensure_members(client: Client, cid: str, cfg: Config) -> list[dict[str, str]]:
    """Reconcile cluster members to the configured admins/users.

    Adds any declared member missing the right role, and removes any existing
    member who is no longer in the config (so taking a user out of the admin/user
    list deletes their cluster membership). The cluster creator/owner -- the
    `<cluster>:creator-cluster-owner` binding -- is always preserved. The whole
    desired set is resolved (_resolve_members) before any binding is touched, so
    a member that cannot be resolved fails the reconciliation rather than having
    their existing binding removed as stale.
    """
    owner_binding = f"{cid}:creator-cluster-owner"
    resolved = _resolve_members(client, cfg)  # pid -> (netid, tier)
    desired_by_role: dict[str, set[str]] = {role: set() for role in ROLE_BY_TIER.values()}
    for pid, (_netid, tier) in resolved.items():
        desired_by_role[ROLE_BY_TIER[tier]].add(pid)

    bindings = client.list_member_bindings(cid)
    current = _by_principal_role(bindings)

    # add missing / correct
    desired_role_for: dict[str, str] = {}
    for role, pids in desired_by_role.items():
        for pid in pids:
            desired_role_for[pid] = role
            if (pid, role) in current:
                netid, _ = resolved[pid]
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
        {"netid": resolved[pid][0], "role": role}
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
        kubectl._run(
            [kubectl.BIN, "--kubeconfig", str(root / "kubeconfig"), "apply", "-f", "-"],
            input=command, capture=True, check=True, timeout=kubectl.MANIFEST_TIMEOUT,
        )
    except subprocess.CalledProcessError as e:
        raise RancherError(f"kubectl apply of import manifest failed: {e.stderr.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise RancherError(
            "applying the Rancher import manifest to the downstream cluster timed out; "
            "the kube-api accepted TCP but never answered. Investigate the cluster and "
            "retry converge"
        ) from e


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
    """Remove the Rancher agent (cattle-system) from the downstream cluster.

    The delete runs with `--wait=false`: namespace deletion is asynchronous and
    can outrun any sane subprocess timeout while its finalizers drain, so the
    command only has to be accepted by the kube-api. The exit status is checked,
    so a rejected delete fails the destroy instead of reporting success.
    """
    if _kubectl(root, "get", "ns", "cattle-system") is None:
        return
    if dry_run():
        action("delete cattle-system namespace (uninstall Rancher agent) via kubectl")
        return
    action("uninstalling Rancher agent: delete cattle-system via kubectl")
    args = [kubectl.BIN, "--kubeconfig", str(root / "kubeconfig"),
            "delete", "ns", "cattle-system", "--wait=false"]
    try:
        proc = kubectl._run(args, capture=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise RancherError(
            f"{kubectl.display(args)} timed out ({kubectl.RUN_TIMEOUT:.0f}s); "
            "investigate the cluster's kube-api and retry destroy"
        ) from e
    if proc.returncode != 0:
        raise RancherError(
            f"uninstalling the Rancher agent failed: {kubectl.display(args)}: "
            f"{(proc.stderr or '').strip()}"
        )


def destroy(ctx: Context, assume_yes: bool = False) -> None:
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)

    cluster = client.find_cluster(cfg.name)
    downstream_id = downstream_rancher_id(ctx.root)
    if cluster is None:
        if downstream_id:
            info(
                f"cluster {cfg.name} has no Rancher cluster, but its cattle-cluster-agent "
                f"is registered ({downstream_id}); removing the orphaned agent from the "
                "downstream cluster"
            )
            log("uninstall the orphaned Rancher agent from the downstream cluster")
            _remove_agent(ctx.root)
            info("done")
            return
        info(f"cluster {cfg.name} not registered in Rancher; nothing to remove")
        return

    if not downstream_id:
        raise RancherError(
            f"Rancher cluster {cfg.name!r} ({cluster.id}) has no cattle-cluster-agent "
            "on the downstream cluster, so it is not this cluster; refuse to delete it"
        )
    if downstream_id != cluster.id:
        raise RancherError(
            f"Rancher cluster {cfg.name!r} ({cluster.id}) does not match the "
            f"downstream cluster ({downstream_id}); refuse to delete it"
        )

    log("delete cluster from Rancher")
    client.delete_cluster(cluster.id)

    log("uninstall Rancher agent from the downstream cluster")
    _remove_agent(ctx.root)
    info("done")


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _orphan_reason(name: str, downstream_id: str) -> str:
    """Explain a downstream agent whose id matches no Rancher cluster by name."""
    return (
        f"the downstream cluster's cattle-cluster-agent is registered as "
        f"{downstream_id}, but no Rancher cluster named {name!r} exists; the "
        "registration was renamed or deleted in the Rancher UI and is now orphaned, "
        "so converge refuses to re-register the cluster under a fresh id. The stale "
        "Rancher cluster is already gone, so deleting the registration will not clear "
        "the downstream agent -- run 'taloscluster plugin rancher destroy' to uninstall it, "
        "then re-run"
    )


def _desired_members(client: Client, cfg: Config) -> dict[str, str]:
    """principal id -> role for every member cluster.yaml declares.

    Cross-tier aliases that resolve to the same principal, and a configured
    member that cannot be resolved at all, are refused here (_resolve_members),
    so check reports a typo'd or unresolvable netid as a failed check instead of
    a clean pass while the configured member is unresolved.
    """
    return {
        pid: ROLE_BY_TIER[tier]
        for pid, (netid, tier) in _resolve_members(client, cfg).items()
    }


def status(ctx: Context) -> dict:
    """What Rancher currently knows about this cluster."""
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)
    downstream_id = downstream_rancher_id(ctx.root)
    cluster = client.find_cluster(cfg.name)
    if cluster is None:
        report = {"registered": False, "url": secrets.rancher_url,
                  "downstream_id": downstream_id}
        if downstream_id:
            report["orphan_reason"] = _orphan_reason(cfg.name, downstream_id)
        return report
    return {
        "registered": True,
        "url": secrets.rancher_url,
        "cluster_id": cluster.id,
        "downstream_id": downstream_id,
        "agent_installed": downstream_id is not None,
        "id_match": downstream_id is not None and downstream_id == cluster.id,
        "members": sorted(
            f"{b.userPrincipalId} ({b.roleTemplateId})"
            for b in client.list_member_bindings(cluster.id)
            if b.userPrincipalId and not b.groupPrincipalId
        ),
    }


def check(ctx: Context) -> dict:
    """Would a converge change anything in Rancher?

    Not ok when the cluster is unregistered, the downstream agent is missing,
    the downstream cattle-cluster-agent id does not match the Rancher cluster id
    (the same refuse-to-attach a converge raises), or the actual bindings differ
    from the desired ones -- the same things converge fixes. A configured member
    that cannot be resolved raises (via _desired_members), so the check reports a
    failed result rather than a clean pass while that user is still unresolved.
    """
    cfg, secrets = _load(ctx.root)
    client = _client(secrets)
    downstream_id = downstream_rancher_id(ctx.root)
    cluster = client.find_cluster(cfg.name)
    if cluster is None:
        if downstream_id:
            return {
                "ok": False,
                "registered": False,
                "downstream_id": downstream_id,
                "agent_installed": True,
                "id_match": False,
                "orphan_reason": _orphan_reason(cfg.name, downstream_id),
            }
        return {"ok": False, "registered": False,
                "reason": f"cluster {cfg.name} is not registered in Rancher"}

    agent = downstream_id is not None
    id_match = downstream_id is not None and downstream_id == cluster.id
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
    if downstream_id is not None and not id_match:
        mismatch = (
            f"Rancher cluster {cfg.name!r} ({cluster.id}) does not match the "
            f"downstream cluster ({downstream_id}); converge refuses this registration"
        )
    else:
        mismatch = ""
    # Publish the id a converge would stamp, not the id of whatever unrelated
    # Rancher cluster happens to bear the configured name. When the downstream
    # agent's id does not match (or there is no agent), the Rancher cluster is
    # not ours -- ArgoCD renders the annotation from this value, so stamping the
    # mismatched Rancher id would drift against the identity converge refuses to
    # attach to. Fall back to the downstream's own id (or none) so both plugins
    # agree on which cluster is ours.
    reported_id = cluster.id if id_match else downstream_id
    return {
        "ok": agent and id_match and not missing and not extra,
        "registered": True,
        "cluster_id": reported_id,
        "downstream_id": downstream_id,
        "agent_installed": agent,
        "id_match": id_match,
        "id_mismatch_reason": mismatch,
        "missing_members": sorted(f"{p} ({r})" for p, r in missing),
        "stale_members": sorted(f"{p} ({r})" for p, r in extra),
    }
