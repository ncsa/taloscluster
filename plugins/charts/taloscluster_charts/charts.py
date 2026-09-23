"""Common values and derived resources for the `charts:` entries.

The metallb ingress pool comes from the taloscluster Context
(`ctx.ingress["metallb"]`) -- taloscluster computes it from cluster.yaml's
`proxmox.network.external.ingress_pool` (or the provider's ingress vip), so it
is never duplicated in the `charts:` config. The traefik service pins the
first address of that pool.
"""

from __future__ import annotations

from typing import Any

import yaml

from .config import CephSecrets, Entry, Namespace

METALLB_POOL_NAME = "external"


def first_pool_address(pool: tuple[str, ...]) -> str:
    """The pool's first allocatable address: "a-b" -> "a", "a" -> "a"."""
    if not pool:
        return ""
    return pool[0].split("-")[0].strip()


# marks a namespace this plugin created, the same managed-by marker the core
# tags provider resources with (taloscluster.naming); disable and destroy
# remove only namespaces carrying it, so one that pre-existed -- or belongs to
# something else entirely -- is never deleted
MANAGED_BY_KEY = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "taloscluster"


def namespace_manifest(ns: Namespace, *, owned: bool = True) -> str:
    """A Namespace document carrying the PSA labels configured for it.

    The managed-by label marks a namespace the plugin created, the marker
    disable and destroy remove on, so it is written for one the plugin is
    creating or already owns and left off (`owned=False`) when converge
    targets a namespace that pre-existed.
    """
    metadata: dict[str, Any] = {"name": ns.name}
    labels = {
        f"pod-security.kubernetes.io/{key}": value
        for key, value in (
            ("enforce", ns.enforce),
            ("audit", ns.audit),
            ("warn", ns.warn),
        )
        if value
    }
    if owned:
        labels[MANAGED_BY_KEY] = MANAGED_BY_VALUE
    if labels:
        metadata["labels"] = labels
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Namespace", "metadata": metadata})


def metallb_pool_manifest(pool: tuple[str, ...], namespace: str) -> str:
    """IPAddressPool + L2Advertisement for the ingress pool.

    MetalLB accepts single IPs and start-end ranges alike, so the pool
    addresses pass through as taloscluster reported them. The CRs live in the
    namespace the chart was installed into, the one its controller watches.
    """
    documents = [
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "IPAddressPool",
            "metadata": {"name": METALLB_POOL_NAME, "namespace": namespace},
            "spec": {"addresses": list(pool), "autoAssign": True},
        },
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "L2Advertisement",
            "metadata": {"name": METALLB_POOL_NAME, "namespace": namespace},
            "spec": {"ipAddressPools": [METALLB_POOL_NAME]},
        },
    ]
    return yaml.safe_dump_all(documents, explicit_start=True, default_flow_style=False)


def common_values(
    entry: Entry, *, ingress_ip: str = "", gateway_enabled: bool = False
) -> dict[str, Any]:
    """The plugin's common values for one chart entry (user values merge on top)."""
    if entry.name == "metallb":
        return {
            "speaker": {"frr": {"enabled": False}},
            "frrk8s": {"enabled": False},
        }

    if entry.name == "traefik":
        values: dict[str, Any] = {
            # the ACME store (/data/acme.json) lives in the pod volume and is
            # not shared between replicas; replicas > 1 would multiply
            # certificate requests until something like cert-manager shares certs
            "deployment": {"replicas": 1},
            "service": {"type": "LoadBalancer"},
            "ports": {
                "web": {
                    "port": 8000,
                    "exposedPort": 80,
                    "protocol": "TCP",
                    "http": {
                        "redirections": {
                            "entryPoint": {"to": "websecure", "scheme": "https", "permanent": True}
                        }
                    },
                },
                # TLS on websecure serves certs from the ingresses' tls
                # secretName (issued by cert-manager); traefik deliberately runs
                # NO acme resolver of its own -- its httpChallenge would
                # intercept /.well-known/acme-challenge for every host it
                # carries a resolver cert for and starve cert-manager's
                # HTTP-01 solvers
                "websecure": {
                    "port": 8443,
                    "exposedPort": 443,
                    "protocol": "TCP",
                },
            },
        }
        if ingress_ip:
            values["service"]["loadBalancerIP"] = ingress_ip
        if gateway_enabled:
            values["providers"] = {"kubernetesGateway": {"enabled": True}}
            values["gateway"] = {
                "enabled": True,
                "listeners": {"web": {"port": 8000, "protocol": "HTTP"}},
            }
        return values

    if entry.name == "cert-manager":
        values = {"crds": {"enabled": True}}
        # ingress-shim defaults: `kubernetes.io/tls-acme: "true"` ingresses pick
        # up the prod (or staging) issuer without extra annotations
        if entry.prod or entry.staging:
            values["ingressShim"] = {
                "defaultIssuerKind": "ClusterIssuer",
                "defaultIssuerGroup": "cert-manager.io",
                "defaultIssuerName": "letsencrypt-prod" if entry.prod else "letsencrypt-staging",
            }
        return values

    if entry.name == "nfs":
        # chmod the mounted subdirs so arbitrary pods can traverse them
        return {"driver": {"mountPermissions": "0777"}}

    if entry.name == "sealed-secrets":
        # kubeseal's default controller name (and the upstream install guide)
        return {"fullnameOverride": "sealed-secrets-controller"}

    # unknown charts start from the chart's own defaults
    return {}


def cert_manager_issuers(entry: Entry) -> str:
    """ClusterIssuers for the letsencrypt provisioners the entry enables.

    `staging` adds letsencrypt-staging (ACME staging directory), `prod` adds
    letsencrypt-prod (ACME production); both solve HTTP-01 through the
    cluster's default ingress class. Empty when neither is enabled.
    """
    documents = []
    for name, server, enabled in (
        ("letsencrypt-staging",
         "https://acme-staging-v02.api.letsencrypt.org/directory", entry.staging),
        ("letsencrypt-prod", "https://acme-v02.api.letsencrypt.org/directory", entry.prod),
    ):
        if not enabled:
            continue
        documents.append(
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "ClusterIssuer",
                "metadata": {"name": name},
                "spec": {
                    "acme": {
                        "email": entry.email,
                        "server": server,
                        "privateKeySecretRef": {"name": f"{name}-account-key"},
                        # the ACME challenge router must beat traefik's own
                        # host routers (priority) and stay on the plain-http
                        # entrypoint, or the HTTP-01 answer never arrives
                        "solvers": [
                            {
                                "http01": {
                                    "ingress": {
                                        "ingressClassName": "traefik",
                                        "ingressTemplate": {
                                            "metadata": {
                                                "annotations": {
                                                    "traefik.ingress.kubernetes.io/router.priority":
                                                        "99999",
                                                    "traefik.ingress.kubernetes.io/frontend-entry-points":
                                                        "web",
                                                }
                                            }
                                        },
                                    }
                                }
                            }
                        ],
                    }
                },
            }
        )
    if not documents:
        return ""
    return yaml.safe_dump_all(documents, explicit_start=True, default_flow_style=False)


def nfs_storage_class_values(entry: Entry, cluster_name: str) -> dict[str, Any]:
    """Chart values for the entry's storage classes, with radiant-style defaults.

    Per class: the export (server/share), a per-cluster subDir pattern so
    several clusters can share one export without colliding, retained
    on-delete data, and Retain/Immediate as the sane storage-class defaults.
    A `defaultClass: true` class carries the is-default-class annotation.
    """
    classes = []
    for sc in entry.storage_classes:
        sub_dir = sc.get("subDir") or (
            f"{cluster_name}/"
            "${pvc.metadata.namespace}-${pvc.metadata.name}-${pv.metadata.name}"
        )
        parameters = {
            "server": sc["server"],
            "share": sc["share"],
            "subDir": sub_dir,
            "onDelete": sc.get("onDelete", "retain"),
            **(sc.get("parameters") or {}),
        }
        class_values: dict[str, Any] = {
            "name": sc["name"],
            "parameters": parameters,
            "reclaimPolicy": sc.get("reclaimPolicy", "Retain"),
            "volumeBindingMode": sc.get("volumeBindingMode", "Immediate"),
            "mountOptions": sc.get("mountOptions", ["nfsvers=4.1"]),
        }
        annotations = dict(sc.get("annotations") or {})
        if sc.get("defaultClass"):
            annotations["storageclass.kubernetes.io/is-default-class"] = "true"
        if annotations:
            class_values["annotations"] = annotations
        classes.append(class_values)
    return {"storageClasses": classes}


# the ceph-csi charts expect these secret names in their own namespaces
CEPH_SECRET_NAMES = {
    "ceph-csi-rbd": "csi-rbd-secret",
    "ceph-csi-cephfs": "csi-cephfs-secret",
}

# both charts ship privileged node plugins (hostPath mounts, ceph clients)
CEPH_PSA = ("privileged", "privileged", "privileged")


# both ceph-csi charts, in driver order. Disable and destroy probe every one
# of them, so a chart whose rbd:/fs: flag was turned off goes away with its
# Secret and namespace instead of staying behind orphaned.
CEPH_CHARTS = ("ceph-csi-rbd", "ceph-csi-cephfs")


def ceph_charts(entry: Entry) -> tuple[str, ...]:
    """The ceph-csi charts the entry enables; release name == chart name."""
    charts: tuple[str, ...] = ()
    if entry.rbd:
        charts += ("ceph-csi-rbd",)
    if entry.fs:
        charts += ("ceph-csi-cephfs",)
    return charts


def ceph_values(entry: Entry) -> dict[str, Any]:
    """The csiConfig every enabled ceph-csi chart shares."""
    return {"csiConfig": [{"clusterID": entry.cluster_id, "monitors": list(entry.monitors)}]}


# the charts' own default StorageClass names
CEPH_CLASS_NAMES = {"ceph-csi-rbd": "csi-rbd-sc", "ceph-csi-cephfs": "csi-cephfs-sc"}


def ceph_storage_class_values(entry: Entry, chart: str) -> dict[str, Any]:
    """storageClass values for one ceph-csi chart from the entry's `rbd:`/`fs:`
    mapping; empty for a bare boolean, which installs the driver alone.

    The class points at the entry's clusterID and the given pool (rbd) or file
    system (fs); Retain is the default reclaim policy, as for nfs, so deleting
    a claim never deletes data. `parameters` are extra `storageClass.*` chart
    values for that class (imageFeatures, mounter, fuseMountOptions, ...).
    """
    sc = entry.rbd_class if chart == "ceph-csi-rbd" else entry.fs_class
    if sc is None:
        return {}
    values: dict[str, Any] = {
        "create": True,
        "name": sc.get("name", CEPH_CLASS_NAMES[chart]),
        "clusterID": entry.cluster_id,
        "reclaimPolicy": sc.get("reclaimPolicy", "Retain"),
    }
    if chart == "ceph-csi-rbd":
        values["pool"] = sc["pool"]
    else:
        values["fsName"] = sc["fsName"]
        if sc.get("pool"):
            values["pool"] = sc["pool"]
    if sc.get("mountOptions"):
        values["mountOptions"] = list(sc["mountOptions"])
    annotations = dict(sc.get("annotations") or {})
    if sc.get("defaultClass"):
        annotations["storageclass.kubernetes.io/is-default-class"] = "true"
    if annotations:
        values["annotations"] = annotations
    values.update(sc.get("parameters") or {})
    return {"storageClass": values}


def ceph_namespace(chart: str) -> Namespace:
    return Namespace(chart, *CEPH_PSA)


def ceph_secret(secrets: CephSecrets, chart: str) -> dict[str, Any]:
    """One ceph-csi chart's csi Secret document.

    A removal probes each Secret alone: a both-charts probe reads one missing
    Secret as all of them gone and would skip the delete entirely.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": CEPH_SECRET_NAMES[chart], "namespace": chart},
        "stringData": {"userID": secrets.user_id, "userKey": secrets.user_key},
    }


def ceph_secrets_manifest(secrets: CephSecrets, entry: Entry) -> str:
    """The csi Secret(s) the enabled charts' provisioners and node plugins read."""
    return yaml.safe_dump_all(
        [ceph_secret(secrets, chart) for chart in ceph_charts(entry)],
        explicit_start=True,
        default_flow_style=False,
    )
