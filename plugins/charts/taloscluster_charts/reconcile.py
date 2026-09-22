"""Converge/status/check/destroy for the `charts:` entries.

`converge` is drift-driven: a chart is installed/upgraded only when the
release is missing, a pinned version differs, or a `latest` entry has a newer
chart version upstream (`helm show chart`, read-only, safe under dry-run).
Resources the plugin applies outside helm (namespaces, Gateway API CRDs, the
metallb pool) are applied only when missing or drifted. `plan` therefore shows
exactly what a sync would change, with the values (secrets redacted) a release
that would install or upgrade gets.
"""

from __future__ import annotations

from typing import Any

import yaml
from taloscluster.context import Context
from taloscluster.errors import ReconcileError, preflight_tools
from taloscluster.output import action, dry_run, info, log, show_yaml, warn

from . import charts, helm, kube, upstream
from .config import Config, Entry, Namespace, is_newer, merge_values

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


def _ingress_pool(ctx: Context) -> tuple[str, ...]:
    pool = ctx.ingress.get("metallb") or []
    return tuple(str(address) for address in pool)


def _gateway_enabled(entries: dict[str, Entry]) -> bool:
    entry = entries.get("gateway")
    return bool(entry and entry.enabled)


def _namespace_of(entry: Entry) -> str:
    return entry.namespace.name if entry.namespace else "default"


def _manifest_urls(entry: Entry) -> tuple[str, ...]:
    """Manifest url(s) for one entry, resolving `latest` from upstream.

    A `version: latest` gateway resolves to the newest gateway-api release at
    call time, so converge and check always look at current upstream: a new
    release changes the url's content and shows up as drift on the next sync.
    Only the known gateway entry has a `{version}` template; explicit urls
    never resolve.
    """
    if not entry.is_latest:
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
    preflight_tools(["helm", "kubectl"])

    reason = _deferred(ctx)
    if reason:
        info(f"charts converge deferred ({reason}); nothing would be installed yet")
        return {"deferred": True, "reason": reason}

    pool = _ingress_pool(ctx)
    gateway_on = _gateway_enabled(cfg.entries)
    if cfg.entries.get("traefik") and cfg.entries["traefik"].enabled and not gateway_on:
        warn(
            "charts: gateway is disabled while traefik is enabled; assuming Gateway API is "
            "already installed in the cluster (traefik's gateway provider may fail without it)"
        )

    result = {
        entry.name: _converge_entry(entry, ctx, pool, gateway_on)
        for entry in _ordered(cfg.entries)
    }
    return {"entries": result}


def _converge_entry(entry: Entry, ctx: Context, pool: tuple[str, ...], gateway_on: bool) -> dict:
    if entry.is_manifest:
        return _converge_manifest(entry, ctx)
    if entry.name == "ceph":
        return _converge_ceph(entry, ctx)
    return _converge_chart(entry, ctx, pool, gateway_on)


def _converge_manifest(entry: Entry, ctx: Context) -> dict:
    urls = _manifest_urls(entry)
    if not entry.enabled:
        if not any(kube.exists(ctx.root, url) for url in urls):
            return {"action": "absent", "kind": "manifest"}
        for url in urls:
            kube.delete(ctx.root, url, label=url)
        return {"action": "removed", "kind": "manifest"}

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


def _converge_chart(entry: Entry, ctx: Context, pool: tuple[str, ...], gateway_on: bool) -> dict:
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
        if record is None:
            return {"action": "absent", "kind": "chart"}
        helm.uninstall(kubeconfig, entry.name, namespace)
        return {"action": "removed", "kind": "chart"}

    if entry.namespace:
        _converge_namespace(entry.namespace, root)

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

    taken, version = _deploy_chart(entry, ctx, entry.name, namespace, merged)
    if entry.name == "metallb":
        _converge_pool(root, pool, namespace)
    if entry.name == "cert-manager":
        _converge_issuers(entry, root, namespace)
    return {"action": taken, "kind": "chart", "version": version, "namespace": namespace}


def _deploy_chart(
    entry: Entry,
    ctx: Context,
    release: str,
    namespace: str,
    merged: dict[str, Any],
    chart: str | None = None,
) -> tuple[str, str]:
    """Install/upgrade one helm release to the desired chart version and values.

    Drift-driven: a missing release installs, a pinned version or a values
    change upgrades, and a `latest` entry upgrades only when the repo offers a
    newer chart version. `chart` is the chart to pull from the entry's repo
    when it is not the entry's own (the ceph entry deploys ceph-csi-rbd and
    ceph-csi-cephfs). Returns (action, version-reported).
    """
    chart = chart or entry.chart_name
    kubeconfig = ctx.kubeconfig
    record = helm.release(kubeconfig, release, namespace)
    latest = None
    if entry.is_latest:
        latest = helm.latest_version(chart, entry.repo or "")
    desired = latest or entry.version or "latest"
    installed = helm.chart_version(record) if record else None
    current_values = helm.get_values(kubeconfig, release, namespace) if record else None

    what = None
    if record is None:
        what = f"install {release} ({desired})"
    elif not entry.is_latest and installed != entry.version:
        what = f"upgrade {release}: {installed} -> {entry.version}"
    elif latest and installed and is_newer(latest, installed):
        what = f"upgrade {release}: {installed} -> {latest}"
    elif current_values is not None and current_values != merged:
        what = f"upgrade {release}: chart values changed"

    if what is None:
        info(f"{release}: chart {installed} up to date")
        taken = "up_to_date"
    else:
        log(what)
        helm.upgrade_install(
            kubeconfig, release, chart, entry.repo or "", namespace, entry.version,
            yaml.safe_dump(merged),
        )
        if dry_run():
            info(f"{release}: values")
            show_yaml(merged)
        taken = "installed" if record is None else "upgraded"

    return taken, installed or desired


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
        if not present:
            return {"action": "absent", "kind": "chart"}
        for chart in reversed(charts_wanted):
            if helm.release(kubeconfig, chart, chart) is not None:
                helm.uninstall(kubeconfig, chart, chart)
        if secrets:
            _delete_ceph_secrets(root, secrets, entry)
        for chart in charts_wanted:
            kube.delete(
                root, "-", label=f"namespace {chart}", input=charts.namespace_manifest(
                    charts.ceph_namespace(chart)
                )
            )
        return {"action": "removed", "kind": "chart"}

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

    shared = merge_values(charts.ceph_values(entry), entry.values)
    charts_result = {}
    taken = "up_to_date"
    for chart in charts_wanted:
        # each chart's StorageClass comes from its own rbd:/fs: mapping
        merged = merge_values(shared, charts.ceph_storage_class_values(entry, chart))
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


def _converge_namespace(namespace: Namespace, root) -> None:
    manifest = charts.namespace_manifest(namespace)
    if kube.exists(root, "-", input=manifest) and kube.matches(root, "-", input=manifest):
        info(f"namespace {namespace.name} up to date")
        return
    if dry_run():
        action(f"kubectl apply namespace {namespace.name}")
        show_yaml(manifest)
        return
    log(f"ensure namespace {namespace.name}")
    kube.apply(root, "-", label=f"namespace {namespace.name}", input=manifest)


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
            applied = entry.enabled and all(
                kube.exists(ctx.root, url) for url in _manifest_urls(entry)
            )
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

    problems: list[str] = []
    upgrade_available: dict[str, str] = {}
    entries: dict[str, str] = {}

    for entry in _ordered(cfg.entries):
        ok, state = _check_entry(entry, ctx, pool, upgrade_available)
        entries[entry.name] = state
        if not ok:
            problems.append(entry.name)

    return {
        "ok": not problems,
        "entries": entries,
        "upgrade_available": upgrade_available,
    }


def _check_entry(
    entry: Entry, ctx: Context, pool: tuple[str, ...], upgrade_available: dict[str, str]
) -> tuple[bool, str]:
    """Whether one entry is as desired, and the state to report for it.

    States: "ok"; "not_installed" (enabled but absent -- needs a converge, not
    drift); "drifted" (present but not as desired); "absent" (disabled and
    gone); "present" (disabled but still there).
    """
    root, kubeconfig = ctx.root, ctx.kubeconfig
    if entry.name == "ceph":
        secrets = entry.ceph_secrets
        for chart in charts.ceph_charts(entry):
            record = helm.release(kubeconfig, chart, chart) if entry.enabled else None
            if not entry.enabled:
                if record is not None:
                    return False, "present"
                continue
            if record is None or record.get("status") != "deployed":
                return False, "not_installed"
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
        still_there = any(kube.exists(root, url) for url in _manifest_urls(entry))
        if not entry.enabled:
            return not still_there, "absent" if not still_there else "present"
        if not still_there:
            return False, "not_installed"
        ok = all(kube.matches(root, url) for url in _manifest_urls(entry))
        return ok, "ok" if ok else "drifted"

    namespace = _namespace_of(entry)
    record = helm.release(kubeconfig, entry.name, namespace) if entry.enabled else None
    if not entry.enabled:
        return record is None, "absent" if record is None else "present"
    if record is None:
        return False, "not_installed"
    if record.get("status") != "deployed":
        return False, "drifted"

    if entry.is_latest:
        latest = helm.latest_version(entry.chart_name, entry.repo or "")
        installed = helm.chart_version(record)
        if latest and installed and is_newer(latest, installed):
            # informational: a newer chart exists, but the release is healthy
            upgrade_available[entry.name] = latest

    if entry.namespace and not kube.matches(
        root, "-", input=charts.namespace_manifest(entry.namespace)
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
            for url in entry.urls():
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
                kube.delete(
                    root,
                    "-",
                    label=f"namespace {chart}",
                    input=charts.namespace_manifest(charts.ceph_namespace(chart)),
                )
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
            kube.delete(
                root,
                "-",
                label=f"namespace {entry.namespace.name}",
                input=charts.namespace_manifest(Namespace(entry.namespace.name)),
            )
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
