"""Generate the two manifests applied to the ArgoCD cluster.

1. A cluster Secret (`argocd.argoproj.io/secret-type: cluster`) so ArgoCD can reach
   this downstream cluster. Built from the downstream cluster's own kubeconfig
   (server + CA + client cert/key).
2. An AppProject with `admin` / `user` roles whose groups are the merged
   rancher + argocd member emails.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import yaml
from taloscluster.context import Context
from taloscluster.errors import ConfigError

from .config import Config, enabled

#: Name of the downstream Secret that carries the Cinder cloud.conf. radiant-cluster's
#: cinder template references this name; keep in sync with any chart you point
#: ``argocd.infra.url`` at.
CINDER_SECRET_NAME = "cinder-csi-cloud-config"
CINDER_NAMESPACE = "cinder-csi"


def downstream_kubeconfig(root: Path) -> dict:
    """Load this cluster's own (gitignored) kubeconfig."""
    path = root / "kubeconfig"
    if not path.is_file():
        raise ConfigError(f"missing this cluster's kubeconfig {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {path}: {e}") from e
    return data


def _yq(value: object) -> str:
    """Render a single-line scalar for YAML interpolation with minimal, safe quoting.

    User-controlled scalars (URLs, emails, versions, ids, server addresses) are
    interpolated through this instead of plain f-string formatting so a value
    containing a colon+space, apostrophe, double quote, leading/trailing space or
    a YAML-reserved word is quoted/escaped by PyYAML rather than producing an
    unparsable manifest or a value that silently changes type on reload. Plain
    scalars dump unquoted, so existing output is unchanged. Always emits a single
    physical line -- a value with an embedded newline or one long enough to fold
    is dumped as an escaped double-quoted line -- so the continuation line never
    lands at the wrong indentation when interpolated.
    """
    dumped = yaml.safe_dump({"k": str(value)}).split("k: ", 1)[1].rstrip("\n")
    # The plain dump folds long scalars and block-quotes embedded newlines, which
    # land on continuation lines at the wrong indentation when the value is
    # interpolated. Fall back to a single double-quoted line (escaped, no
    # folding) whenever the plain scalar spans more than one physical line.
    if "\n" not in dumped:
        return dumped
    return yaml.safe_dump(str(value), default_style='"', width=10**9).rstrip("\n")


def _cluster_connection(root: Path) -> tuple[str, str | None, str | None, str | None]:
    """(server, ca_data, client_cert_data, client_key_data) from downstream kubeconfig."""
    kc = downstream_kubeconfig(root)
    try:
        cluster = kc["clusters"][0]["cluster"]
    except (KeyError, IndexError, TypeError):
        raise ConfigError("kubeconfig has no cluster entry") from None
    server = cluster.get("server", "")
    ca = cluster.get("certificate-authority-data")
    user = None
    for u in kc.get("users", []) or []:
        if u.get("name", "").startswith("admin") or u.get("user"):
            user = u.get("user", {})
            break
    cert = client_key = None
    if isinstance(user, dict):
        cert = user.get("client-certificate-data")
        client_key = user.get("client-key-data")
    return server, ca, cert, client_key


def _cluster_secret(cfg: Config, ctx: Context) -> str:
    server, ca, cert, key = _cluster_connection(ctx.root)
    tls: dict[str, object] = {"insecure": False}
    if ca:
        tls["caData"] = ca
    if cert:
        tls["certData"] = cert
    if key:
        tls["keyData"] = key
    config = json.dumps({"tlsClientConfig": tls})
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: argocd-{cfg.name}-secret
  labels:
    argocd.argoproj.io/secret-type: cluster
{_rancher_annotation(ctx)}  namespace: argocd
type: Opaque
stringData:
  name: {cfg.name}
  server: {_yq(server)}
  config: |
    {config}
"""


def _rancher_annotation(ctx: Context) -> str:
    """Stamp the Rancher cluster id onto the ArgoCD cluster Secret, when known.

    The rancher plugin runs first (argocd declares AFTER = ("rancher",)) and puts
    its cluster id in ctx.results, so the ArgoCD entry can be traced back to the
    Rancher cluster. Absent when rancher is not installed or not configured for
    this cluster -- that is normal, not an error.
    """
    cluster_id = _rancher_id(ctx)
    if not cluster_id:
        return ""
    return f"  annotations:\n    rancher.cattle.io/cluster-id: {_yq(cluster_id)}\n"


def _rancher_id(ctx: Context) -> str:
    return str((ctx.results.get("rancher") or {}).get("cluster_id") or "")


def _groups_block(emails: tuple[str, ...]) -> str:
    if not emails:
        return ""
    items = "\n".join(f"    - {_yq(e)}" for e in emails)
    return f"    groups:\n{items}"


def _role(name: str, description: str, policy: str, emails: tuple[str, ...]) -> str:
    groups = _groups_block(emails)
    return f"""\
  - name: {name}
    description: {description}
    policies:
    - {policy}
{groups}"""


def _project(cfg: Config, ctx: Context) -> str:
    server, _ca, _cert, _key = _cluster_connection(ctx.root)
    name = cfg.name
    admin = _role(
        "admin",
        f"Admin privileges to {name}",
        f"p, proj:{name}:admin, applications, *, {name}/*, allow",
        cfg.members.admins,
    )
    user = _role(
        "user",
        f"Read-only privileges to {name}",
        f"p, proj:{name}:user, applications, get, {name}/*, allow",
        cfg.members.users,
    )
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: {name}
  namespace: argocd
spec:
  description: {name} cluster
  sourceRepos:
  - '*'
  destinations:
  - namespace: '*'
    server: {_yq(server)}
  - namespace: argocd
    server: https://kubernetes.default.svc
  clusterResourceWhitelist:
  - group: '*'
    kind: '*'
  roles:
{admin}
{user}
"""


def _repo_secret(cfg: Config, git: tuple[str, str] | None) -> str:
    """The repository Secret (secret-type: repository) for this cluster's git repo."""
    if not cfg.git_url:
        raise ConfigError("argocd.git.url not set in cluster.yaml; cannot render repo secret")
    username, password = git or ("", "")
    # Build stringData through a YAML serializer so every scalar (the git
    # credentials most importantly) round-trips exactly: a token containing an
    # apostrophe, backslash, newline or YAML-looking text is quoted/escaped by
    # PyYAML instead of producing an unparsable manifest.
    string_data = {
        "project": cfg.name,
        "name": f"{cfg.name}-cluster",
        "url": cfg.git_url,
        "type": "git",
        "username": username,
        "password": password,
    }
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: repo-{cfg.name}\n"
        "  namespace: argocd\n"
        "  labels:\n"
        "    argocd.argoproj.io/secret-type: repository\n"
        "stringData:\n"
        + textwrap.indent(yaml.safe_dump(string_data, default_flow_style=False), "  ")
    )


def _root_app(cfg: Config) -> str:
    """The root Application (app-of-apps) that deploys charts/apps on this cluster."""
    if not cfg.git_url:
        raise ConfigError("argocd.git.url not set in cluster.yaml; cannot render apps.yaml")
    name = cfg.name
    automated = (
        "    automated:\n"
        "      prune: true\n"
        "      selfHeal: true\n"
        "      allowEmpty: false\n"
    ) if cfg.automated else ""
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
  labels:
    cluster: {name}
    app: {name}
  namespace: argocd
spec:
  project: {name}
  destination:
    server: https://kubernetes.default.svc
    namespace: argocd
  syncPolicy:
{automated}    syncOptions:
      - CreateNamespace=true
  source:
    repoURL: {_yq(cfg.git_url)}
    path: charts/apps
    targetRevision: HEAD
    helm:
      version: v3
      releaseName: {name}
      values: |
        cluster: {name}
"""


def _cluster_apps(cfg: Config, ctx: Context) -> str:
    """The cluster apps Application (`<cluster>-cluster`) embedding per-cluster values.

    Mirrors ncsa/radiant-cluster `charts/apps/values.yaml` with **everything
    disabled** by default; only identity fields (cluster name/url, git repo) are
    populated. Enable features later by filling in config. The embedded Helm
    values are built as a dict and rendered through a YAML serializer so every
    user-controlled scalar (URLs, emails, versions, IPs, classes) round-trips
    exactly whatever shape it takes instead of corrupting the values block.
    """
    if not cfg.git_url:
        raise ConfigError("argocd.git.url not set in cluster.yaml; cannot render cluster-apps")
    if not cfg.infra_url:
        raise ConfigError("argocd.infra.url not set in cluster.yaml; cannot render cluster-apps")
    server, _ca, _cert, _key = _cluster_connection(ctx.root)
    name = cfg.name
    rancher_id = _rancher_id(ctx)
    openstack_url = cfg.openstack.url if cfg.openstack else ""
    openstack_project = ctx.openstack.get("project", "")
    openstack_region = (
        cfg.openstack.region
        if cfg.openstack and cfg.openstack.region
        else ctx.openstack.get("region") or "RegionOne"
    )
    metallb_enabled = enabled(cfg.metallb)
    # MetalLB addresses: a bare single IP (OpenStack VIP) becomes a /32; the
    # Proxmox ingress_pool range ("start-end") is already a valid MetalLB spec.
    ingress_vip = ctx.ingress.get("vip", "")
    metallb_addresses: list[str] = []
    if metallb_enabled:
        pool = ctx.ingress.get("metallb") or ([ingress_vip] if ingress_vip else [])
        for addr in pool:
            addr = str(addr).strip()
            if not addr:
                continue
            metallb_addresses.append(
                addr if ("-" in addr or "/" in addr) else f"{addr}/32"
            )
    ingress_enabled = enabled(cfg.ingress)
    ingress_class = cfg.ingress.get("class") or "traefik"
    certmanager_enabled = enabled(cfg.certmanager)
    certmanager_email = cfg.certmanager.get("email") or ""
    certmanager_class = ingress_class
    sealedsecrets_enabled = enabled(cfg.sealedsecrets)
    cinder_enabled = enabled(cfg.cinder)
    nfs_enabled = enabled(cfg.nfs)
    monitoring_enabled = enabled(cfg.monitoring)
    # argocd.nfs.servers is passed through verbatim (name -> server/path/defaultClass)
    nfs_servers = cfg.nfs.get("servers") if nfs_enabled else None
    sync_enabled = cfg.sync
    automated = (
        "    automated:\n"
        "      allowEmpty: false\n"
        "      prune: true\n"
        "      selfHeal: true\n"
    ) if cfg.automated else ""

    # The embedded values, mirroring charts/apps/values.yaml with features off by
    # default. `version` keys are omitted entirely unless explicitly set; the
    # chart keeps its own default then.
    values: dict[str, Any] = {
        "cluster": {
            "name": name,
            "url": server,
            "rancher": {"id": rancher_id},
        },
        "openstack": {
            "project": openstack_project,
            "auth_url": openstack_url,
            "region": openstack_region,
        },
        "notifications": {},
        "sync": sync_enabled,
        "metallb": {
            "enabled": metallb_enabled,
            "addresses": metallb_addresses,
        },
        "certmanager": {
            "enabled": certmanager_enabled,
            "email": certmanager_email,
            "class": certmanager_class,
        },
        "ingresscontroller": {
            "enabled": ingress_enabled,
            "class": ingress_class,
            "publicIP": ctx.ingress.get("floating_ip", ""),
            "privateIP": ingress_vip,
            "traefik": {
                "storageClass": "",
                "ports": {},
            },
        },
        "gateway_crd": {"enabled": True},
        "sealedsecrets": {"enabled": sealedsecrets_enabled},
        "monitoring": {"enabled": monitoring_enabled},
        "healthmonitor": {
            "enabled": False,
            "targetRevision": "HEAD",
            "nfs": False,
            "notifiers": {"console": {"report": "change", "threshold": 0}},
        },
        "nfs": {
            "enabled": nfs_enabled,
            "type": "csi",
            "mountPermissions": "0777",
        },
        "longhorn": {"enabled": False, "replicas": 3},
        "cinder": {"enabled": cinder_enabled},
        "manila": {"enabled": False, "protocols": []},
        "raw": {"enabled": True, "resources": [], "templates": []},
    }
    if metallb_version := cfg.metallb.get("version"):
        values["metallb"]["version"] = metallb_version
    if certmanager_version := cfg.certmanager.get("version"):
        values["certmanager"]["version"] = certmanager_version
    if traefik_version := (cfg.ingress.get("traefik") or {}).get("version"):
        values["ingresscontroller"]["traefik"]["version"] = traefik_version
    if sealedsecrets_version := cfg.sealedsecrets.get("version"):
        values["sealedsecrets"]["version"] = sealedsecrets_version
    if cinder_version := cfg.cinder.get("version"):
        values["cinder"]["version"] = cinder_version
    if isinstance(nfs_servers, dict) and nfs_servers:
        values["nfs"]["servers"] = nfs_servers
    values_yaml = textwrap.indent(
        yaml.safe_dump(values, default_flow_style=False), " " * 8
    )

    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  annotations: null
  labels:
    app: infrastructure
    cluster: {name}
  name: {name}-cluster
  namespace: argocd
spec:
  destination:
    namespace: argocd
    server: https://kubernetes.default.svc
  project: {name}
  source:
    helm:
      releaseName: {name}
      values: |
{values_yaml}
      version: v3
    path: charts/apps
    repoURL: {_yq(cfg.infra_url)}
    targetRevision: HEAD
  syncPolicy:
{automated}    syncOptions:
    - CreateNamespace=true
"""


def cinder_namespace() -> str:
    """The ``cinder-csi`` Namespace the cloud.conf Secret lives in.

    Delivered separately from the Secret on converge so the Secret can be applied
    on a cluster where the namespace does not exist yet (first converge) without
    the Namespace being part of the Secret manifest that `check` compares or
    `destroy` removes. ArgoCD's app-of-apps syncs the Namespace itself
    (CreateNamespace=true / selfHeal), so it is not owned by the plugin beyond
    creation and kubectl apply is idempotent.
    """
    return f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {CINDER_NAMESPACE}
"""


def _cinder_secret(cfg: Config, ctx: Context, ost: tuple[str, str]) -> str:
    """A Secret holding the Cinder cloud.conf, delivered to the downstream cluster.

    The (updated) infra chart's cinder application references this Secret through
    the upstream cinder-csi chart's ``secret.enabled=true, secret.create=false,
    secret.name=<name>, secret.filename=cloud.conf``; with ``create=false`` the
    chart mounts the existing Secret instead of embedding the credential in its
    own values, so the application credential never appears in any ArgoCD
    Application. Called only when credentials are present (the caller raises a
    ConfigError otherwise); the caller (converge) delivers the Secret's
    ``cinder-csi`` Namespace first.
    """
    cred_id, cred_secret = ost
    auth_url = (
        cfg.openstack.url
        if cfg.openstack and cfg.openstack.url
        else ctx.openstack.get("url", "")
    )
    region = (
        cfg.openstack.region
        if cfg.openstack and cfg.openstack.region
        else ctx.openstack.get("region") or "RegionOne"
    )
    cloud_conf = (
        f"[Global]\n"
        f"auth-url={auth_url}\n"
        f"region={region}\n"
        f"application-credential-id={cred_id}\n"
        f"application-credential-secret={cred_secret}\n"
    )
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: {CINDER_SECRET_NAME}
  namespace: {CINDER_NAMESPACE}
type: Opaque
stringData:
  cloud.conf: |-
{textwrap.indent(cloud_conf, "    ")}
"""


def render(
    cfg: Config,
    ctx: Context,
    git: tuple[str, str] | None = None,
    ost: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Return rendered manifests: secret / project / repo / apps / cluster-apps.

    The ingress VIP / floating ip and the OpenStack project come off `ctx` --
    taloscluster computed them during the same converge. They used to be read by
    shelling out to `clusterctl status`, which stopped existing when the tool was
    renamed and failed silently, blanking `metallb.addresses` and both ingress
    IPs.
    """
    out = {
        "secret": _cluster_secret(cfg, ctx),
        "project": _project(cfg, ctx),
    }
    if cfg.git_url or git:
        out["repo"] = _repo_secret(cfg, git)
    if cfg.git_url:
        out["apps"] = _root_app(cfg)
        out["cluster-apps"] = _cluster_apps(cfg, ctx)
        if enabled(cfg.cinder):
            if ost is None:
                raise ConfigError(
                    "argocd.cinder.enabled requires an OpenStack application "
                    "credential; set openstack.credential_id and "
                    "openstack.credential_secret in secrets.yaml"
                )
            out["cinder-secret"] = _cinder_secret(cfg, ctx, ost)
    return out
