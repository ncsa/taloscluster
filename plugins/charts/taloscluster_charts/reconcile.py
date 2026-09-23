"""Converge/status/check/destroy for the `charts:` entries.

`converge` is drift-driven: a chart is installed/upgraded only when the
release is missing, a pinned version differs, or a `latest` entry has a newer
chart version upstream (`helm show chart`, read-only, safe under dry-run).
Resources the plugin applies outside helm (namespaces, Gateway API CRDs, the
metallb pool) are applied only when missing or drifted. `plan` therefore shows
exactly what a sync would change, with the values (secrets redacted) a release
that would install or upgrade gets, and `check` reports that same drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from taloscluster.context import Context
from taloscluster.errors import ConfigError, ReconcileError, preflight_tools
from taloscluster.output import action, dry_run, info, log, show_yaml, warn

from . import charts, helm, kube, upstream
from .config import Config, Entry, Namespace, is_newer, merge_values, same_version

# dependency order: gateway CRDs before traefik's gateway provider, the
# metallb chart (and its pool) before traefik claims an address from it; the
# rest have no dependencies among the known entries
ORDER = (
    "gateway",
    "metallb",
    "traefik",
    "sealed-secrets",
    "cert-manager",
    "ceph",
    "nfs",
)


def _ordered(entries: dict[str, Entry]) -> list[Entry]:
    known = [entries[name] for name in ORDER if name in entries]
    extra = sorted(name for name in entries if name not in ORDER)
    return known + [entries[name] for name in extra]


def _deferred(ctx: Context) -> str | None:
    """Why converge is deferred during a plan before bootstrap (mirrors argocd).

    Nothing can be installed until converge writes the kubeconfig at bootstrap,
    so a plan must report the charts as deferred rather than fail on a missing
    kubeconfig.
    """
    if not dry_run():
        return None
    if not ctx.kubeconfig.is_file():
        return "this cluster has no kubeconfig yet (it is written at bootstrap)"
    return None


def still_installed(ctx: Context) -> bool:
    """Whether any disabled entry still has something in the cluster to remove.

    The plugin's activation check calls this so a section whose every entry is
    disabled -- exactly what `taloscluster init` scaffolds -- keeps the plugin
    (and its helm requirement) inactive, while a disabled entry with a release
    or applied manifest left stays configured until converge removes it.
    Best-effort: only a bootstrapped cluster (a kubeconfig present) can hold
    anything, and a probe that cannot answer -- helm or kubectl missing, the
    kube-api unreachable, a `latest` manifest that cannot be named -- reads as
    nothing left rather than keeping the plugin active.
    """
    if not ctx.kubeconfig.is_file():
        return False
    try:
        cfg = Config.load(ctx.root)
    except ConfigError:
        return False
    for entry in cfg.entries.values():
        if entry.enabled:
            continue
        try:
            if entry.is_manifest:
                if any(kube.exists(ctx.root, url) for url in entry.urls()):
                    return True
            elif entry.name == "ceph":
                if any(
                    helm.release(ctx.kubeconfig, chart, chart)
                    for chart in charts.ceph_charts(entry)
                ):
                    return True
            elif helm.release(ctx.kubeconfig, entry.name, _namespace_of(entry)):
                return True
        except (ConfigError, OSError, ReconcileError):
            continue
    return False


def _ingress_pool(ctx: Context) -> tuple[str, ...]:
    pool = ctx.ingress.get("metallb") or []
    return tuple(str(address) for address in pool)


def _gateway_enabled(entries: dict[str, Entry]) -> bool:
    entry = entries.get("gateway")
    return bool(entry and entry.enabled)


def _namespace_of(entry: Entry) -> str:
    return entry.namespace.name if entry.namespace else "default"


def _merged_values(
    entry: Entry, ctx: Context, pool: tuple[str, ...], gateway_on: bool
) -> dict[str, Any]:
    """The chart values an entry converges to (shared by converge and check)."""
    common = charts.common_values(
        entry,
        ingress_ip=charts.first_pool_address(pool) if entry.name == "traefik" else "",
        gateway_enabled=entry.name == "traefik" and gateway_on,
    )
    merged = merge_values(common, entry.values)
    if entry.name == "nfs":
        # the storage classes are chart values, built from the structured
        # entry schema with the cluster name woven into the subDir pattern
        merged = merge_values(merged, charts.nfs_storage_class_values(entry, ctx.cfg.name))
    return merged


def _ceph_chart_values(entry: Entry, chart: str) -> dict[str, Any]:
    """The values one ceph-csi chart converges to: the shared csiConfig and
    user values, plus the StorageClass that chart's own rbd:/fs: mapping builds."""
    shared = merge_values(charts.ceph_values(entry), entry.values)
    return merge_values(shared, charts.ceph_storage_class_values(entry, chart))


def _manifest_urls(entry: Entry) -> tuple[str, ...]:
    """Manifest url(s) for one entry, resolving `latest` from upstream.

    A `version: latest` gateway resolves to the newest gateway-api release at
    call time, so converge and check always look at current upstream: a new
    release changes the url's content and shows up as drift on the next sync.
    Only the known gateway entry has a `{version}` template; explicit urls
    never resolve and never touch upstream.
    """
    if not entry.is_latest or not any("{version}" in url for url in entry.manifest):
        return entry.urls()
    if entry.name == "gateway":
        latest = upstream.gateway_latest_version()
        if latest:
            return entry.urls(resolved=latest)
    raise ReconcileError(
        f"{entry.name}: cannot resolve the latest release; pin a version in cluster.yaml or retry"
    )


# ---------------------------------------------------------------------------
# converge
# ---------------------------------------------------------------------------


def converge(ctx: Context, assume_yes: bool = False) -> dict:
    cfg = Config.load(ctx.root)

    # the deferral comes first, so a plan on a cluster that has not bootstrapped
    # yet reports the charts as deferred instead of failing on a missing helm
    reason = _deferred(ctx)
    if reason:
        info(f"charts converge deferred ({reason}); nothing would be installed yet")
        return {"deferred": True, "reason": reason}

    preflight_tools(["helm", "kubectl"])

    pool = _ingress_pool(ctx)
    gateway_on = _gateway_enabled(cfg.entries)
    if cfg.entries.get("traefik") and cfg.entries["traefik"].enabled and not gateway_on:
        warn(
            "charts: gateway is disabled while traefik is enabled; assuming Gateway API is "
            "already installed in the cluster (traefik's gateway provider may fail without it)"
        )

    # one broken entry (an unresolvable `latest`, a failed apply) must not
    # stop the others: converge them all, then fail if any did
    result: dict[str, dict] = {}
    failed: list[str] = []
    for entry in _ordered(cfg.entries):
        try:
            result[entry.name] = _converge_entry(entry, ctx, cfg.entries, pool, gateway_on)
        except ReconcileError as e:
            warn(f"charts: {e}")
            failed.append(entry.name)
    if failed:
        raise ReconcileError(f"charts: {', '.join(failed)} failed to converge")
    return {"entries": result}


def _converge_entry(
    entry: Entry, ctx: Context, entries: dict[str, Entry], pool: tuple[str, ...], gateway_on: bool
) -> dict:
    if entry.is_manifest:
        return _converge_manifest(entry, ctx)
    if entry.name == "ceph":
        return _converge_ceph(entry, ctx)
    return _converge_chart(entry, ctx, entries, pool, gateway_on)


def _converge_manifest(entry: Entry, ctx: Context) -> dict:
    if not entry.enabled:
        # removing needs the applied urls; when the version cannot be
        # resolved (unreachable GitHub) there is nothing to name, so the
        # entry reports absent instead of failing the run
        try:
            urls = _manifest_urls(entry)
        except ReconcileError:
            return {"action": "absent", "kind": "manifest"}
        if not any(kube.exists(ctx.root, url) for url in urls):
            return {"action": "absent", "kind": "manifest"}
        for url in urls:
            kube.delete(ctx.root, url, label=url)
        return {"action": "removed", "kind": "manifest"}

    urls = _manifest_urls(entry)
    changed = False
    for url in urls:
        # exists-first: a broken/unreachable url then surfaces as an apply
        # error instead of a confusing diff failure
        if kube.exists(ctx.root, url) and kube.matches(ctx.root, url):
            info(f"{entry.name}: manifest up to date ({url})")
            continue
        kube.apply(ctx.root, url, label=url)
        changed = True
    return {
        "action": "applied" if changed else "unchanged",
        "kind": "manifest",
        "manifests": list(urls),
    }


def _converge_chart(
    entry: Entry, ctx: Context, entries: dict[str, Entry], pool: tuple[str, ...], gateway_on: bool
) -> dict:
    root, kubeconfig = ctx.root, ctx.kubeconfig
    namespace = _namespace_of(entry)
    record = helm.release(kubeconfig, entry.name, namespace)

    if not entry.enabled:
        # derived resources go first: the CRs need the chart's CRDs, and the
        # deletes need the chart's webhook still serving
        if entry.name == "metallb" and pool:
            _delete_pool(root, pool)
        if entry.name == "cert-manager":
            _delete_issuers(root, entry)
        if record is not None:
            helm.uninstall(kubeconfig, entry.name, namespace)
        if entry.namespace:
            _delete_namespace(
                root, entry.namespace.name, shared_with=_namespace_sharers(entries, entry)
            )
        return {"action": "removed" if record is not None else "absent", "kind": "chart"}

    if entry.namespace:
        _converge_namespace(entry.namespace, root)

    merged = _merged_values(entry, ctx, pool, gateway_on)

    taken, version = _deploy_chart(entry, ctx, entry.name, namespace, merged)
    if entry.name == "metallb":
        _converge_pool(root, pool, namespace)
    if entry.name == "cert-manager":
        _converge_issuers(entry, root, namespace)
    return {"action": taken, "kind": "chart", "version": version, "namespace": namespace}


@dataclass(frozen=True)
class _Drift:
    """What converge would do to one helm release, shared by converge and check.

    kind is "install" for a missing release, "upgrade" for one converge would
    upgrade, "newer" for a `latest` entry with a newer upstream chart
    (informational), and None when the release is as desired.
    """

    kind: str | None
    detail: str               # the version to install, the upgrade's why, or the newer version
    record: dict | None       # the release's `helm list` record
    installed: str | None     # the installed chart version
    desired: str              # the chart version converge targets ("latest" when unpinned)


def _chart_drift(
    entry: Entry,
    kubeconfig: Path,
    release: str,
    namespace: str,
    merged: dict[str, Any],
    chart: str | None = None,
) -> _Drift:
    """The one drift decision for a helm release, shared by converge and check.

    Converge acts on it (`_deploy_chart`) and check reports it, so the two can
    never disagree: a missing release installs, a non-`deployed` status, a
    pinned version differing from the installed chart -- compared without the
    leading `v` some charts tag, like cert-manager's v1.21.2 -- or changed
    values upgrade, and a `latest` entry with a newer upstream chart is only
    reported as available. `chart` is the chart to look up when it is not the
    entry's own (the ceph entry deploys ceph-csi-rbd and ceph-csi-cephfs).
    """
    chart = chart or entry.chart_name
    record = helm.release(kubeconfig, release, namespace)
    latest = helm.latest_version(chart, entry.repo or "") if entry.is_latest else None
    current_values = helm.get_values(kubeconfig, release, namespace) if record else None
    status = record.get("status") if record else None
    installed = helm.chart_version(record) if record else None
    desired = latest or entry.version or "latest"

    if record is None:
        return _Drift("install", desired, record, installed, desired)
    if status != "deployed":
        return _Drift("upgrade", f"status is {status}", record, installed, desired)
    if not entry.is_latest and not same_version(installed, entry.version):
        return _Drift("upgrade", f"{installed} -> {entry.version}", record, installed, desired)
    if current_values is not None and current_values != merged:
        return _Drift("upgrade", "chart values changed", record, installed, desired)
    if latest and installed and is_newer(latest, installed):
        return _Drift("newer", latest, record, installed, desired)
    return _Drift(None, "", record, installed, desired)


def _deploy_chart(
    entry: Entry,
    ctx: Context,
    release: str,
    namespace: str,
    merged: dict[str, Any],
    chart: str | None = None,
) -> tuple[str, str]:
    """Install/upgrade one helm release to the desired chart version and values.

    Drift-driven, on the decision `_chart_drift` shares with check: a missing
    release installs, a pinned version or a values change upgrades, a `latest`
    entry upgrades only when the repo offers a newer chart version, and a
    release that is not `deployed` is retried: a failed release upgrades in
    place, while one stuck `pending-*` or `uninstalling` (an interrupted run)
    is uninstalled first -- helm refuses to upgrade over it -- and installed
    fresh. `chart` is the chart to pull from the entry's repo when it is not
    the entry's own (the ceph entry deploys ceph-csi-rbd and ceph-csi-cephfs).
    Returns (action, version-reported).
    """
    chart = chart or entry.chart_name
    kubeconfig = ctx.kubeconfig
    drift = _chart_drift(entry, kubeconfig, release, namespace, merged, chart=chart)

    if drift.kind is None:
        info(f"{release}: chart {drift.installed} up to date")
        taken = "up_to_date"
    else:
        if drift.kind == "install":
            what = f"install {release} ({drift.detail})"
        elif drift.kind == "newer":
            what = f"upgrade {release}: {drift.installed} -> {drift.detail}"
        else:
            what = f"upgrade {release}: {drift.detail}"
        log(what)
        # helm refuses to upgrade over a pending-* or uninstalling release
        # ("another operation (install/upgrade/rollback) is in progress");
        # the only way out is to clear it and install fresh
        status = drift.record.get("status") if drift.record else None
        if status and (status.startswith("pending-") or status == "uninstalling"):
            helm.uninstall(kubeconfig, release, namespace)
        helm.upgrade_install(
            kubeconfig, release, chart, entry.repo or "", namespace, entry.version,
            yaml.safe_dump(merged),
        )
        if dry_run():
            info(f"{release}: values")
            show_yaml(merged)
        taken = "installed" if drift.record is None else "upgraded"

    return taken, drift.installed or drift.desired


def _converge_ceph(entry: Entry, ctx: Context) -> dict:
    """Deploy the enabled ceph-csi charts from the single `ceph` entry.

    Each chart gets its own privileged namespace (release name == chart name),
    the shared csiConfig, and -- when the entry carries userID/userKey (usually
    from secrets.yaml) -- its CephX Secret delivered here so the provisioners
    can actually work.
    """
    root, kubeconfig = ctx.root, ctx.kubeconfig
    charts_wanted = charts.ceph_charts(entry)
    secrets = entry.ceph_secrets

    if not entry.enabled:
        present = any(
            helm.release(kubeconfig, chart, chart) is not None for chart in charts_wanted
        )
        for chart in reversed(charts_wanted):
            if helm.release(kubeconfig, chart, chart) is not None:
                helm.uninstall(kubeconfig, chart, chart)
        if secrets:
            _delete_ceph_secrets(root, secrets, entry)
        for chart in charts_wanted:
            _delete_namespace(root, chart)
        return {"action": "removed" if present else "absent", "kind": "chart"}

    for chart in charts_wanted:
        _converge_namespace(charts.ceph_namespace(chart), root)
    if secrets:
        _converge_ceph_secrets(root, secrets, entry)
    else:
        names = ", ".join(charts.CEPH_SECRET_NAMES[c] for c in charts_wanted)
        warn(
            f"ceph: no charts.ceph.userID/userKey configured; {names} must exist on the cluster "
            "(however you manage them) or the provisioners cannot reach ceph"
        )

    charts_result = {}
    taken = "up_to_date"
    for chart in charts_wanted:
        # each chart's StorageClass comes from its own rbd:/fs: mapping
        merged = _ceph_chart_values(entry, chart)
        action, _ = _deploy_chart(entry, ctx, chart, chart, merged, chart=chart)
        charts_result[chart] = action
        if action != "up_to_date":
            taken = action
    return {"action": taken, "kind": "chart", "charts": charts_result}


def _converge_ceph_secrets(root, secrets, entry: Entry) -> None:
    """Deliver the csi secrets only when missing or drifted."""
    manifest = charts.ceph_secrets_manifest(secrets, entry)
    if kube.exists(root, "-", input=manifest) and kube.matches(root, "-", input=manifest):
        info("ceph: csi secrets up to date")
        return
    if dry_run():
        action("kubectl apply ceph csi secrets")
        show_yaml(manifest)
        return
    log("apply ceph csi secrets")
    kube.apply(root, "-", label="ceph csi secrets", input=manifest)


def _delete_ceph_secrets(root, secrets, entry: Entry) -> None:
    manifest = charts.ceph_secrets_manifest(secrets, entry)
    if kube.exists(root, "-", input=manifest):
        kube.delete(root, "-", label="ceph csi secrets", input=manifest)


def _converge_pool(root, pool: tuple[str, ...], namespace: str) -> None:
    """Apply the IPAddressPool/L2Advertisement after the chart owns the CRDs."""
    if not pool:
        info("metallb: taloscluster exposes no ingress pool; skipping pool resources")
        return
    manifest = charts.metallb_pool_manifest(pool)
    # exists-first: on a cluster without the chart (plan, fresh install) the
    # metallb CRDs are absent and even a diff cannot answer
    if kube.exists(root, "-", input=manifest) and kube.matches(root, "-", input=manifest):
        info("metallb: pool resources up to date")
        return
    if dry_run():
        action("kubectl apply IPAddressPool/L2Advertisement (metallb-system)")
        show_yaml(manifest)
        return
    log("apply metallb pool (IPAddressPool, L2Advertisement)")
    # the CRs are validated by a webhook served by the controller pod, which is
    # still coming up seconds after a fresh install
    if not kube.wait_deployment_available(root, "metallb-controller", namespace):
        info("metallb: controller not reported available; applying anyway")
    kube.apply(root, "-", label="metallb pool resources", input=manifest)


def _namespace_manifest_for(root, namespace: Namespace) -> str:
    """The namespace manifest converge and check target on this cluster.

    A namespace that does not exist yet, or one already carrying the
    managed-by label -- the plugin created it -- is converged to the full,
    labelled manifest; a namespace that pre-existed without the label is
    converged to its PSA labels only, so the ownership marker disable and
    destroy delete on is never stamped onto a namespace the plugin did not
    create. A failed label read counts as not ours: the marker is never
    added on a guess.
    """
    full = charts.namespace_manifest(namespace)
    if not kube.exists(root, "-", input=full):
        return full
    labels = kube.namespace_labels(root, namespace.name)
    if labels is not None and labels.get(charts.MANAGED_BY_KEY) == charts.MANAGED_BY_VALUE:
        return full
    return charts.namespace_manifest(namespace, owned=False)


def _converge_namespace(namespace: Namespace, root) -> None:
    manifest = _namespace_manifest_for(root, namespace)
    if kube.exists(root, "-", input=manifest) and kube.matches(root, "-", input=manifest):
        info(f"namespace {namespace.name} up to date")
        return
    if dry_run():
        action(f"kubectl apply namespace {namespace.name}")
        show_yaml(manifest)
        return
    log(f"ensure namespace {namespace.name}")
    kube.apply(root, "-", label=f"namespace {namespace.name}", input=manifest)


# namespaces a cluster cannot live without; the api server refuses to delete
# them and that failure would abort the rest of the run
PROTECTED_NAMESPACES = frozenset({"default", "kube-system", "kube-public"})


def _namespace_sharers(entries: dict[str, Entry], entry: Entry) -> tuple[str, ...]:
    """The other enabled entries converging into this entry's namespace.

    A disable may not remove a namespace another enabled entry still uses;
    destroy passes none, since every entry goes and nothing shares it after.
    """
    if not entry.namespace:
        return ()
    return tuple(
        name
        for name, other in entries.items()
        if name != entry.name
        and other.enabled
        and other.namespace
        and other.namespace.name == entry.namespace.name
    )


def _delete_namespace(root, name: str, *, shared_with: tuple[str, ...] = ()) -> None:
    """Delete a namespace the plugin created, and only such a namespace.

    The plugin's namespace manifests carry the managed-by label, so one that
    pre-existed or was created by something else -- which never carries it --
    is left alone, as is a namespace another enabled entry still converges
    into. The cluster's own namespaces are never deleted: the api server
    refuses that and the failure would abort the rest of the run.
    """
    if name in PROTECTED_NAMESPACES:
        warn(f"charts: namespace {name} is one of the cluster's own; leaving it in place")
        return
    if shared_with:
        info(f"namespace {name} is still used by {', '.join(shared_with)}; leaving it in place")
        return
    labels = kube.namespace_labels(root, name)
    if labels is None:
        return
    if labels.get(charts.MANAGED_BY_KEY) != charts.MANAGED_BY_VALUE:
        info(f"namespace {name} was not created by the plugin; leaving it in place")
        return
    kube.delete(
        root, "-", label=f"namespace {name}", input=charts.namespace_manifest(Namespace(name))
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def status(ctx: Context) -> dict:
    cfg = Config.load(ctx.root)
    preflight_tools(["helm", "kubectl"])
    pool = _ingress_pool(ctx)

    entries: dict[str, Any] = {}
    for entry in _ordered(cfg.entries):
        if entry.is_manifest:
            applied = False
            if entry.enabled:
                try:
                    applied = all(kube.exists(ctx.root, url) for url in _manifest_urls(entry))
                except ReconcileError:
                    applied = False  # unresolvable latest: cannot probe the urls
            entries[entry.name] = {"kind": "manifest", "enabled": entry.enabled, "applied": applied}
            continue
        if entry.name == "ceph":
            charts_result: dict[str, Any] = {}
            for chart in charts.ceph_charts(entry):
                record = helm.release(ctx.kubeconfig, chart, chart) if entry.enabled else None
                charts_result[chart] = {
                    "release": (record or {}).get("name", ""),
                    "status": (record or {}).get("status", ""),
                    "chart": helm.chart_version(record) if record else "",
                }
            entries[entry.name] = {
                "kind": "chart",
                "enabled": entry.enabled,
                "charts": charts_result,
                "secrets_managed": entry.ceph_secrets is not None,
            }
            continue
        record = (
            helm.release(ctx.kubeconfig, entry.name, _namespace_of(entry))
            if entry.enabled
            else None
        )
        entries[entry.name] = {
            "kind": "chart",
            "enabled": entry.enabled,
            "release": (record or {}).get("name", ""),
            "status": (record or {}).get("status", ""),
            "chart": helm.chart_version(record) if record else "",
            "app_version": (record or {}).get("app_version", ""),
        }

    report: dict[str, Any] = {"entries": entries}
    if "metallb" in cfg.entries and pool:
        report["metallb_pool_present"] = kube.exists(
            ctx.root, "-", input=charts.metallb_pool_manifest(pool)
        )
    if "cert-manager" in cfg.entries and charts.cert_manager_issuers(cfg.entries["cert-manager"]):
        report["cert_manager_issuers_present"] = kube.exists(
            ctx.root, "-", input=charts.cert_manager_issuers(cfg.entries["cert-manager"])
        )
    return report


def check(ctx: Context) -> dict:
    cfg = Config.load(ctx.root)
    preflight_tools(["helm", "kubectl"])
    pool = _ingress_pool(ctx)
    gateway_on = _gateway_enabled(cfg.entries)

    problems: list[str] = []
    upgrade_available: dict[str, str] = {}
    entries: dict[str, str] = {}

    for entry in _ordered(cfg.entries):
        ok, state = _check_entry(entry, ctx, pool, gateway_on, upgrade_available)
        entries[entry.name] = state
        if not ok:
            problems.append(entry.name)

    return {
        "ok": not problems,
        "entries": entries,
        "upgrade_available": upgrade_available,
    }


def _check_entry(
    entry: Entry,
    ctx: Context,
    pool: tuple[str, ...],
    gateway_on: bool,
    upgrade_available: dict[str, str],
) -> tuple[bool, str]:
    """Whether one entry is as desired, and the state to report for it.

    States: "ok"; "not_installed" (enabled but absent -- needs a converge, not
    drift); "drifted" (present but not as desired); "absent" (disabled and
    gone); "present" (disabled but still there). Chart releases are judged by
    the same drift decision converge acts on (`_chart_drift`), so a state here
    means a converge would change something.
    """
    root, kubeconfig = ctx.root, ctx.kubeconfig
    if entry.name == "ceph":
        secrets = entry.ceph_secrets
        for chart in charts.ceph_charts(entry):
            if not entry.enabled:
                if helm.release(kubeconfig, chart, chart) is not None:
                    return False, "present"
                continue
            drift = _chart_drift(
                entry, kubeconfig, chart, chart, _ceph_chart_values(entry, chart), chart=chart
            )
            if drift.kind == "install":
                return False, "not_installed"
            if drift.kind == "upgrade":
                return False, "drifted"
            if drift.kind == "newer":
                upgrade_available[chart] = drift.detail
        if entry.enabled and secrets and not kube.matches(
            root, "-", input=charts.ceph_secrets_manifest(secrets, entry)
        ):
            return False, "drifted"
        return True, "ok"
    if entry.is_manifest:
        if entry.name == "gateway" and not entry.is_latest:
            # a pinned gateway reports a newer upstream release; a `latest`
            # gateway already tracks it (drift comes from the url's content)
            latest = upstream.gateway_latest_version()
            if latest and is_newer(latest, entry.version):
                upgrade_available[entry.name] = latest
        try:
            urls = _manifest_urls(entry)
        except ReconcileError:
            # the urls cannot be resolved (unreachable GitHub, air-gap): a
            # lookup failure must not fail check, so a disabled entry reports
            # absent and an enabled one not_installed (converge fails on it
            # with the same message)
            return not entry.enabled, "absent" if not entry.enabled else "not_installed"
        still_there = any(kube.exists(root, url) for url in urls)
        if not entry.enabled:
            return not still_there, "absent" if not still_there else "present"
        if not still_there:
            return False, "not_installed"
        ok = all(kube.matches(root, url) for url in urls)
        return ok, "ok" if ok else "drifted"

    namespace = _namespace_of(entry)
    if not entry.enabled:
        record = helm.release(kubeconfig, entry.name, namespace)
        return record is None, "absent" if record is None else "present"

    merged = _merged_values(entry, ctx, pool, gateway_on)
    drift = _chart_drift(entry, kubeconfig, entry.name, namespace, merged)
    if drift.kind == "install":
        return False, "not_installed"
    if drift.kind == "upgrade":
        return False, "drifted"
    if drift.kind == "newer":
        # informational: a newer chart exists, but the release is healthy
        upgrade_available[entry.name] = drift.detail

    if entry.namespace and not kube.matches(
        root, "-", input=_namespace_manifest_for(root, entry.namespace)
    ):
        return False, "drifted"
    if entry.name == "metallb" and pool:
        ok = kube.matches(root, "-", input=charts.metallb_pool_manifest(pool))
        return ok, "ok" if ok else "drifted"
    if entry.name == "cert-manager":
        manifest = charts.cert_manager_issuers(entry)
        if manifest and not kube.matches(root, "-", input=manifest):
            return False, "drifted"
    return True, "ok"


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def destroy(ctx: Context, assume_yes: bool = False) -> None:
    cfg = Config.load(ctx.root)
    preflight_tools(["helm", "kubectl"])
    root, kubeconfig = ctx.root, ctx.kubeconfig
    pool = _ingress_pool(ctx)

    for entry in reversed(_ordered(cfg.entries)):
        if entry.is_manifest:
            # exists-guard: deleting an already-gone manifest set fails on
            # missing kinds even with --ignore-not-found
            try:
                urls = _manifest_urls(entry)
            except ReconcileError as e:
                # destroy must take the rest of the cluster down even when a
                # `latest` manifest cannot be named (unreachable GitHub)
                warn(f"charts: {e}")
                continue
            for url in urls:
                if kube.exists(root, url):
                    kube.delete(root, url, label=url)
            continue
        if entry.name == "ceph":
            for chart in reversed(charts.ceph_charts(entry)):
                if helm.release(kubeconfig, chart, chart) is not None:
                    helm.uninstall(kubeconfig, chart, chart)
            secrets = entry.ceph_secrets
            if secrets:
                _delete_ceph_secrets(root, secrets, entry)
            for chart in charts.ceph_charts(entry):
                _delete_namespace(root, chart)
            continue
        namespace = _namespace_of(entry)
        if entry.name == "metallb" and pool:
            # the CRs go first: helm uninstall removes the metallb.io CRDs,
            # and a delete of a kind the api no longer knows errors out
            _delete_pool(root, pool)
        if entry.name == "cert-manager":
            # the deletes go through the chart's validating webhook, which
            # must still be serving
            _delete_issuers(root, entry)
        if helm.release(kubeconfig, entry.name, namespace) is not None:
            helm.uninstall(kubeconfig, entry.name, namespace)
        if entry.namespace:
            _delete_namespace(root, entry.namespace.name)
    info("done")


def _delete_pool(root, pool: tuple[str, ...]) -> None:
    """Delete the pool CRs, skipping cleanly when they (or their CRDs) are gone."""
    manifest = charts.metallb_pool_manifest(pool)
    if kube.exists(root, "-", input=manifest):
        kube.delete(root, "-", label="metallb pool resources", input=manifest)


def _converge_issuers(entry: Entry, root, namespace: str) -> None:
    """Apply the letsencrypt ClusterIssuers after the chart owns the CRDs."""
    manifest = charts.cert_manager_issuers(entry)
    if not manifest:
        info("cert-manager: no staging/prod provisioners enabled")
        return
    if kube.exists(root, "-", input=manifest) and kube.matches(root, "-", input=manifest):
        info("cert-manager: ClusterIssuers up to date")
        return
    if dry_run():
        action("kubectl apply letsencrypt ClusterIssuers")
        show_yaml(manifest)
        return
    log("apply cert-manager ClusterIssuers")
    # the ClusterIssuers are validated by the chart's webhook, which is still
    # coming up seconds after a fresh install
    if not kube.wait_deployment_available(root, "cert-manager", namespace):
        info("cert-manager: webhook not reported ready; applying anyway")
    kube.apply(root, "-", label="cert-manager ClusterIssuers", input=manifest)


def _delete_issuers(root, entry: Entry) -> None:
    """Delete the entry's ClusterIssuers while the chart's webhook still serves."""
    manifest = charts.cert_manager_issuers(entry)
    if manifest and kube.exists(root, "-", input=manifest):
        kube.delete(root, "-", label="cert-manager ClusterIssuers", input=manifest)
