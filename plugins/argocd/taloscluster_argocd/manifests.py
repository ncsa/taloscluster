"""Generate the two manifests applied to the ArgoCD cluster.

1. A cluster Secret (`argocd.argoproj.io/secret-type: cluster`) so ArgoCD can reach
   this downstream cluster. Built from the downstream cluster's own kubeconfig
   (server + CA + client cert/key).
2. An AppProject with `admin` / `user` roles whose groups are the merged
   rancher + argocd member emails.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from taloscluster.context import Context
from taloscluster.errors import ConfigError

from .config import Config, enabled

# Git repo hosting the radiant infrastructure charts (charts/apps) used by the
# `<cluster>-cluster` app-of-apps.
INFRA_REPO = "https://git.ncsa.illinois.edu/kubernetes/radiant-cluster.git"


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
  server: {server}
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
    cluster_id = (ctx.results.get("rancher") or {}).get("cluster_id")
    if not cluster_id:
        return ""
    return f"  annotations:\n    rancher.cattle.io/cluster-id: {cluster_id}\n"


def _groups_block(emails: tuple[str, ...]) -> str:
    if not emails:
        return ""
    items = "\n".join(f"    - {e}" for e in emails)
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
        f"p, proj:{name}:read-only, applications, get, {name}/*, allow",
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
    server: {server}
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
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: repo-{cfg.name}
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  project: {cfg.name}
  name: {cfg.name}-cluster
  url: {cfg.git_url}
  type: git
  username: '{username}'
  password: '{password}'
"""


def _root_app(cfg: Config) -> str:
    """The root Application (app-of-apps) that deploys charts/apps on this cluster."""
    if not cfg.git_url:
        raise ConfigError("argocd.git.url not set in cluster.yaml; cannot render apps.yaml")
    name = cfg.name
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
    automated:
      prune: true
      selfHeal: true
      allowEmpty: false
    syncOptions:
      - CreateNamespace=true
  source:
    repoURL: {cfg.git_url}
    path: charts/apps
    targetRevision: HEAD
    helm:
      version: v3
      releaseName: {name}
      values: |
        cluster: {name}
"""


def _version_line(section: dict, indent: str = "          ") -> str:
    """Render a `version:` line only when explicitly set in the config.

    Anything not present in cluster.yaml keeps the chart's default by omitting
    the key entirely.
    """
    v = section.get("version")
    if not v:
        return ""
    return f'{indent}version: "{v}"\n'


def _cluster_apps(
    cfg: Config, ctx: Context, ost: tuple[str, str] | None = None
) -> str:
    """The cluster apps Application (`<cluster>-cluster`) embedding per-cluster values.

    Mirrors ncsa/radiant-cluster `charts/apps/values.yaml` with **everything
    disabled** by default; only identity fields (cluster name/url, git repo) are
    populated. Enable features later by filling in config.
    """
    if not cfg.git_url:
        raise ConfigError("argocd.git.url not set in cluster.yaml; cannot render cluster-apps")
    server, _ca, _cert, _key = _cluster_connection(ctx.root)
    name = cfg.name
    openstack_url = cfg.openstack.url if cfg.openstack else ""
    ost_id, ost_secret = (ost or ("", ""))
    metallb_enabled = enabled(cfg.metallb)
    metallb_addr = ctx.ingress.get("vip", "")
    metallb_addresses = ""
    if metallb_enabled and metallb_addr:
        metallb_addresses = f"          - {metallb_addr}/32\n"
    ingress_enabled = enabled(cfg.ingress)
    ingress_class = cfg.ingress.get("class") or "traefik"
    certmanager_enabled = enabled(cfg.certmanager)
    certmanager_email = cfg.certmanager.get("email") or ""
    certmanager_class = ingress_class
    sealedsecrets_enabled = enabled(cfg.sealedsecrets)
    cinder_enabled = enabled(cfg.cinder)
    nfs_enabled = enabled(cfg.nfs)
    nfs_taiga = bool(cfg.nfs.get("taiga")) and nfs_enabled
    nfs_project = ctx.openstack.get("project", "")
    nfs_servers = "          servers: {}\n"
    if nfs_taiga:
        nfs_servers = (
            "          servers:\n"
            "            taiga:\n"
            "              server: taiga-nfs.ncsa.illinois.edu\n"
            f'              path: "/taiga/ncsa/radiant/{nfs_project}"\n'
            "              defaultClass: true\n"
        )
    sync_enabled = cfg.sync
    metallb_version = _version_line(cfg.metallb)
    certmanager_version = _version_line(cfg.certmanager)
    traefik_version = _version_line(cfg.ingress.get("traefik") or {}, indent="            ")
    sealedsecrets_version = _version_line(cfg.sealedsecrets)
    cinder_version = _version_line(cfg.cinder)
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
        cluster:
          name: {name}
          url: {server}
          rancher:
            id: ""

        openstack:
          project: {name}
          auth_url: {openstack_url}
          region: RegionOne
          credential_id: "{ost_id}"
          credential_secret: "{ost_secret}"

        notifications: {{}}

        sync: {"true" if sync_enabled else "false"}

        metallb:
          enabled: {"true" if metallb_enabled else "false"}
{metallb_version}          addresses:
{metallb_addresses}        certmanager:
          enabled: {"true" if certmanager_enabled else "false"}
{certmanager_version}          email: "{certmanager_email}"
          class: {certmanager_class}

        ingresscontroller:
          enabled: {"true" if ingress_enabled else "false"}
          class: {ingress_class}
          publicIP: "{ctx.ingress.get("floating_ip", "")}"
          privateIP: "{metallb_addr}"
          traefik:
{traefik_version}            storageClass: ""
            ports: {{}}

        gateway_crd:
          enabled: true

        sealedsecrets:
          enabled: {"true" if sealedsecrets_enabled else "false"}
{sealedsecrets_version}
        monitoring:
          enabled: false

        healthmonitor:
          enabled: false
          targetRevision: HEAD
          nfs: false
          notifiers:
            console:
              report: change
              threshold: 0

        nfs:
          enabled: {"true" if nfs_enabled else "false"}
          type: subdir
          mountPermissions: "0777"
{nfs_servers}
        longhorn:
          enabled: false
          replicas: 3

        cinder:
          enabled: {"true" if cinder_enabled else "false"}
{cinder_version}
        manila:
          enabled: false
          protocols: []

        raw:
          enabled: true
          resources: []
          templates: []
      version: v3
    path: charts/apps
    repoURL: {INFRA_REPO}
    targetRevision: HEAD
  syncPolicy:
    automated:
      allowEmpty: false
      prune: true
      selfHeal: true
    syncOptions:
    - CreateNamespace=true
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
        out["cluster-apps"] = _cluster_apps(cfg, ctx, ost)
    return out
