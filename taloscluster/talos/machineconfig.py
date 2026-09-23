"""Build each node's Talos machine config.

The shared machine-config patches become Python dicts dumped to YAML files and
stacked as `--config-patch` on `talosctl gen config`, in this order:
  machine -> hostname -> (disk encryption) -> (cluster, controlplane only) ->
  firewall -> (kubespan) -> tailscale -> freeform.

The patches follow the document layout the `--talos-version` given to gen
config emits: the v1alpha1 fields up to 1.13, and from 1.14 on the typed
documents (KubeNodeConfig, KubeletConfig, UnattendedInstallConfig) that own
those fields -- a config that patches the v1alpha1 homes there is rejected.
Converge picks the layout from the version each node RUNS, since configs are
applied before the Talos upgrade phase.

Kept as separate patch files on purpose: hostname (HostnameConfig) and tailscale
(ExtensionServiceConfig) are their own machine-config documents, and the
hostname patch relies on the `$patch: delete` directive to drop the `auto` field
-- both only behave correctly as standalone patch docs, never dump_all'd into
one stream.

This module is provider-neutral. Everything a specific infrastructure backend
needs -- the install disk, provider networking, provider static pods -- arrives
as a :class:`~taloscluster.infrastructure.TalosContribution` built by that
backend, and is stacked after the shared patches but before the user's freeform
patches so an explicit user override still wins.
"""

from __future__ import annotations

import copy
import re
import secrets
import tempfile
from pathlib import Path

import yaml

from .. import versions
from ..config import DEFAULT_MTU, KUBESPAN_PORT, Config, ConfigError, Machine
from ..infrastructure import Endpoint, TalosContribution
from . import talosctl

# Vendored deliberately. These were previously pulled from GitHub by URLs a
# maintainer can move -- the cert-approver at a release tag, metrics-server at
# a release asset -- so an upstream push could change what every new control
# plane runs, and the approver holds CSR-approval RBAC. The cert-approver did
# exactly that once: it crashlooped on exit code 2, and because talosctl
# upgrade-k8s waits for every bootstrap manifest to reconcile, a broken add-on
# blocks kubernetes upgrades entirely. Both now ride cluster.inlineManifests on
# control planes, so what a cluster runs is fixed by this file: the approver at
# its v0.11.0 standalone-install.yaml, metrics-server at its v0.9.0
# components.yaml. Bump these on purpose, not by drift.
CERT_APPROVER_MANIFEST = """\
apiVersion: v1
kind: Namespace
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/warn: restricted
  name: kubelet-serving-cert-approver
---
apiVersion: v1
kind: ServiceAccount
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: kubelet-serving-cert-approver
  namespace: kubelet-serving-cert-approver
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: certificates:kubelet-serving-cert-approver
rules:
- apiGroups:
  - certificates.k8s.io
  resources:
  - certificatesigningrequests
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - certificates.k8s.io
  resources:
  - certificatesigningrequests/approval
  verbs:
  - update
- apiGroups:
  - authorization.k8s.io
  resources:
  - subjectaccessreviews
  verbs:
  - create
- apiGroups:
  - certificates.k8s.io
  resourceNames:
  - kubernetes.io/kubelet-serving
  resources:
  - signers
  verbs:
  - approve
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: events:kubelet-serving-cert-approver
rules:
- apiGroups:
  - ""
  resources:
  - events
  verbs:
  - create
  - patch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: events:kubelet-serving-cert-approver
  namespace: default
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: events:kubelet-serving-cert-approver
subjects:
- kind: ServiceAccount
  name: kubelet-serving-cert-approver
  namespace: kubelet-serving-cert-approver
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: kubelet-serving-cert-approver
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: certificates:kubelet-serving-cert-approver
subjects:
- kind: ServiceAccount
  name: kubelet-serving-cert-approver
  namespace: kubelet-serving-cert-approver
---
apiVersion: v1
kind: Service
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: kubelet-serving-cert-approver
  namespace: kubelet-serving-cert-approver
spec:
  ports:
  - name: metrics
    port: 9090
    protocol: TCP
    targetPort: metrics
  selector:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
---
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app.kubernetes.io/instance: kubelet-serving-cert-approver
    app.kubernetes.io/name: kubelet-serving-cert-approver
  name: kubelet-serving-cert-approver
  namespace: kubelet-serving-cert-approver
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: kubelet-serving-cert-approver
      app.kubernetes.io/name: kubelet-serving-cert-approver
  template:
    metadata:
      labels:
        app.kubernetes.io/instance: kubelet-serving-cert-approver
        app.kubernetes.io/name: kubelet-serving-cert-approver
    spec:
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - preference:
              matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: DoesNotExist
            weight: 100
      containers:
      - args:
        - serve
        env:
        - name: NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        image: ghcr.io/alex1989hu/kubelet-serving-cert-approver:0.11.0
        imagePullPolicy: Always
        livenessProbe:
          httpGet:
            path: /healthz
            port: health
          initialDelaySeconds: 6
        name: cert-approver
        ports:
        - containerPort: 8080
          name: health
        - containerPort: 9090
          name: metrics
        readinessProbe:
          httpGet:
            path: /readyz
            port: health
          initialDelaySeconds: 3
        resources:
          limits:
            cpu: 250m
            memory: 32Mi
          requests:
            cpu: 10m
            memory: 16Mi
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
          privileged: false
          readOnlyRootFilesystem: true
          runAsNonRoot: true
      priorityClassName: system-cluster-critical
      securityContext:
        fsGroup: 65534
        runAsGroup: 65534
        runAsUser: 65534
        seccompProfile:
          type: RuntimeDefault
      serviceAccountName: kubelet-serving-cert-approver
      tolerations:
      - effect: NoSchedule
        key: node.cloudprovider.kubernetes.io/uninitialized
        operator: Exists
      - effect: NoSchedule
        key: node-role.kubernetes.io/master
        operator: Exists
      - effect: NoSchedule
        key: node-role.kubernetes.io/control-plane
        operator: Exists
"""
METRICS_SERVER_MANIFEST = """\
apiVersion: v1
kind: ServiceAccount
metadata:
  labels:
    k8s-app: metrics-server
  name: metrics-server
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  labels:
    k8s-app: metrics-server
    rbac.authorization.k8s.io/aggregate-to-admin: "true"
    rbac.authorization.k8s.io/aggregate-to-edit: "true"
    rbac.authorization.k8s.io/aggregate-to-view: "true"
  name: system:aggregated-metrics-reader
rules:
- apiGroups:
  - metrics.k8s.io
  resources:
  - pods
  - nodes
  verbs:
  - get
  - list
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  labels:
    k8s-app: metrics-server
  name: system:metrics-server
rules:
- apiGroups:
  - ""
  resources:
  - nodes/metrics
  verbs:
  - get
- apiGroups:
  - ""
  resources:
  - pods
  - nodes
  verbs:
  - get
  - list
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  labels:
    k8s-app: metrics-server
  name: metrics-server-auth-reader
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: extension-apiserver-authentication-reader
subjects:
- kind: ServiceAccount
  name: metrics-server
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  labels:
    k8s-app: metrics-server
  name: metrics-server:system:auth-delegator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
- kind: ServiceAccount
  name: metrics-server
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  labels:
    k8s-app: metrics-server
  name: system:metrics-server
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:metrics-server
subjects:
- kind: ServiceAccount
  name: metrics-server
  namespace: kube-system
---
apiVersion: v1
kind: Service
metadata:
  labels:
    k8s-app: metrics-server
  name: metrics-server
  namespace: kube-system
spec:
  ports:
  - appProtocol: https
    name: https
    port: 443
    protocol: TCP
    targetPort: https
  selector:
    k8s-app: metrics-server
---
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    k8s-app: metrics-server
  name: metrics-server
  namespace: kube-system
spec:
  selector:
    matchLabels:
      k8s-app: metrics-server
  strategy:
    rollingUpdate:
      maxUnavailable: 0
  template:
    metadata:
      labels:
        k8s-app: metrics-server
    spec:
      containers:
      - args:
        - --cert-dir=/tmp
        - --secure-port=10250
        - --kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname
        - --kubelet-use-node-status-port
        - --metric-resolution=15s
        image: registry.k8s.io/metrics-server/metrics-server:v0.9.0
        imagePullPolicy: IfNotPresent
        livenessProbe:
          failureThreshold: 3
          httpGet:
            path: /livez
            port: https
            scheme: HTTPS
          periodSeconds: 10
        name: metrics-server
        ports:
        - containerPort: 10250
          name: https
          protocol: TCP
        readinessProbe:
          failureThreshold: 3
          httpGet:
            path: /readyz
            port: https
            scheme: HTTPS
          initialDelaySeconds: 20
          periodSeconds: 10
        resources:
          requests:
            cpu: 100m
            memory: 200Mi
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
          readOnlyRootFilesystem: true
          runAsNonRoot: true
          runAsUser: 1000
          seccompProfile:
            type: RuntimeDefault
        volumeMounts:
        - mountPath: /tmp
          name: tmp-dir
      nodeSelector:
        kubernetes.io/os: linux
      priorityClassName: system-cluster-critical
      serviceAccountName: metrics-server
      volumes:
      - emptyDir: {}
        name: tmp-dir
---
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  labels:
    k8s-app: metrics-server
  name: v1beta1.metrics.k8s.io
spec:
  group: metrics.k8s.io
  groupPriorityMinimum: 100
  insecureSkipTLSVerify: true
  service:
    name: metrics-server
    namespace: kube-system
  version: v1beta1
  versionPriority: 100
"""
EXTRA_MANIFESTS = [
    {"name": "kubelet-serving-cert-approver", "contents": CERT_APPROVER_MANIFEST},
    {"name": "metrics-server", "contents": METRICS_SERVER_MANIFEST},
]


# The LUKS2 passphrase for machine.systemDiskEncryption lives in the cluster's
# talossecrets.yaml under this key, written there when converge generates the
# secrets and never regenerated -- like the rest of that file it is the
# cluster's irreplaceable identity, and losing it means losing the encrypted
# disks. A secrets file without the key is a cluster created before system disk
# encryption: its machines were installed unencrypted, so no configuration may
# carry the encryption settings -- Talos reads the EPHEMERAL encryption config
# from the live machine config on every boot and refuses a mismatch, so the
# settings must only ever reach machines installed (or reinstalled) with them.
DISK_PASSPHRASE_KEY = "diskEncryptionPassphrase"


def with_disk_passphrase(raw: str) -> str:
    """A fresh secrets bundle with a generated disk-encryption passphrase.

    Appends one top-level key to the `talosctl gen secrets` output rather than
    round-tripping the YAML, so the bundle keeps its generated shape; talosctl
    ignores unknown keys when it loads the bundle.
    """
    return f"{raw.rstrip()}\n{DISK_PASSPHRASE_KEY}: {secrets.token_urlsafe(32)}\n"


def disk_passphrase(secrets_path: Path) -> str | None:
    """The LUKS2 passphrase stored in the cluster's machine secrets, or None.

    Anything unreadable, not a mapping, or without the key reads as None: the
    machine configs then carry no encryption settings, which is exactly right
    for a cluster whose secrets predate them.
    """
    try:
        data = yaml.safe_load(secrets_path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(DISK_PASSPHRASE_KEY)
    return value if isinstance(value, str) and value else None


def _disk_encryption_patch(passphrase: str) -> dict:
    """machine.systemDiskEncryption: STATE and EPHEMERAL as LUKS2, both keyed
    with the cluster's static passphrase in slot 0."""
    def luks2() -> dict:
        return {
            "provider": "luks2",
            "keys": [{"slot": 0, "static": {"passphrase": passphrase}}],
        }

    return {
        "machine": {
            "systemDiskEncryption": {"state": luks2(), "ephemeral": luks2()},
        }
    }


def _label_value(value: str) -> str:
    """Make a value safe as a kubernetes label value: spaces become `_`
    (an OpenStack project name may contain spaces, a label value may not)."""
    return str(value).replace(" ", "_")


def _node_labels(m: Machine, default_tags: dict[str, str] | None) -> dict[str, str]:
    """role/pool first, then defaults (project name), then cluster.yaml tags —
    later wins, so a user tag can override a default."""
    labels = {"ncsa/role": m.role, "ncsa/pool": m.pool}
    labels.update(default_tags or {})
    labels.update(m.tags)
    return {k: _label_value(v) for k, v in labels.items()}


# The first Talos minor whose generated config moves the machine and cluster
# fields below into typed config documents; a config generated for an older
# minor keeps the v1alpha1 fields, and a node refuses the layout its running
# Talos does not know.
TYPED_DOCUMENTS_MINOR = 14


def typed_documents(talos_version: str) -> bool:
    """Whether a config generated for this Talos version carries the typed
    document layout (1.14+) instead of the v1alpha1 fields."""
    parsed = versions.parse(talos_version)
    return len(parsed) >= 2 and parsed[1] >= TYPED_DOCUMENTS_MINOR


def _machine_patch(m: Machine, cfg: Config, install_disk: str,
                   default_tags: dict[str, str] | None = None,
                   node_cidr: str | None = None,
                   talos_version: str | None = None) -> dict | list[dict]:
    """The machine patch(es) for one node, in the layout `talos_version`'s gen
    config emits (the cluster's target when not given).

    The v1alpha1 layout (up to 1.13) patches the classic fields; from 1.14 the
    same settings ride the typed documents that own them -- patching the
    v1alpha1 homes there is rejected as already set. The install patch only
    adds `wipe`: `--install-disk`/`--install-image` fill the disk and image on
    the v1alpha1 layout and the installer image and disk selector on the typed
    one, and the UnattendedInstallConfig merge drops the generated disk
    selector unless the patch restates it. The endpoint certSANs are no patch
    at all: `--additional-sans` fills machine.certSANs and the API server's on
    both layouts. `node_cidr` keys the pod node IP on a node sitting off the
    cluster network -- a metal server's own L2.
    """
    node_ip = {"validSubnets": [node_cidr or cfg.network.cluster.cidr]}
    if typed_documents(talos_version or cfg.talos_version):
        return [
            {
                "apiVersion": "v1alpha1",
                "kind": "KubeNodeConfig",
                "labels": _node_labels(m, default_tags),
                "nodeIP": node_ip,
            },
            {
                "apiVersion": "v1alpha1",
                "kind": "KubeletConfig",
                "extraArgs": {"rotate-server-certificates": "true"},
            },
            {
                "apiVersion": "v1alpha1",
                "kind": "UnattendedInstallConfig",
                "provisioning": {
                    "diskSelector": {"match": f'disk.dev_path == "{install_disk}"'},
                    "wipe": True,
                },
            },
            {"machine": {"time": {"servers": cfg.network.ntp}}},
        ]
    return {
        "machine": {
            "nodeLabels": _node_labels(m, default_tags),
            "kubelet": {
                "extraArgs": {"rotate-server-certificates": True},
                "nodeIP": node_ip,
            },
            "install": {"wipe": True},
            "time": {"servers": cfg.network.ntp},
        }
    }


def _hostname_patch(m: Machine) -> dict:
    # force the instance name as hostname; strategic merge can't delete a field,
    # so drop `auto` with the $patch: delete directive
    return {
        "apiVersion": "v1alpha1",
        "kind": "HostnameConfig",
        "auto": {"$patch": "delete"},
        "hostname": m.name,
    }


def _cluster_patch(cfg: Config, node_cidr: str | None = None,
                   talos_version: str | None = None) -> dict:
    cluster: dict = {
        "inlineManifests": EXTRA_MANIFESTS,
        # keep etcd peering on the private network, off tailscale; a node
        # off the cluster network (a metal group) advertises its own L2
        "etcd": {"advertisedSubnets": [node_cidr or cfg.network.cluster.cidr]},
    }
    if not typed_documents(talos_version or cfg.talos_version):
        # 1.14's generated KubeNodeConfig already taints control planes
        # NoSchedule; the v1alpha1 flag is refused there as already set
        cluster["allowSchedulingOnControlPlanes"] = False
    return {"cluster": cluster}


# Ports the tailscale extension answers on for direct (non-relayed) peers.
TAILSCALE_PORT = 41641
DHCP_CLIENT_PORT = 68


def _network_rule(name: str, protocol: str, ports: list, subnets: list[str]) -> dict:
    return {
        "apiVersion": "v1alpha1",
        "kind": "NetworkRuleConfig",
        "name": name,
        "portSelector": {"ports": ports, "protocol": protocol},
        "ingress": [{"subnet": subnet} for subnet in subnets],
    }


def _firewall_docs(cfg: Config, node_cidr: str | None = None) -> list[dict]:
    """The Talos ingress firewall, mirroring the provider security rules.

    Same policy as the OpenStack security group and the Proxmox VM firewall:
    everything from every L2 the cluster's nodes sit on, the open-by-default
    ports from anywhere, each `security:` rule's port from its hosts, and
    nothing else. Talos allows loopback, established/related, rate-limited ICMP
    and pod/service traffic on its own; DHCP replies and tailscale's
    direct-connection port are opened here because a node must keep its lease
    and its tailnet reachability while the default action is block. When nodes
    sit on more than one L2 (a `metal` group), KubeSpan's WireGuard port is
    also opened from the other L2s explicitly. `node_cidr` keys the stack on a
    node sitting off the cluster network -- a metal server's own L2.
    """
    subnets = cfg.intra_cluster_cidrs(node_cidr)
    peers = [subnet for subnet in subnets if subnet != (node_cidr or cfg.network.cluster.cidr)]
    docs: list[dict] = [
        {"apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block"},
        _network_rule("cluster-tcp", "tcp", ["1-65535"], subnets),
        _network_rule("cluster-udp", "udp", ["1-65535"], subnets),
        _network_rule("dhcp-client", "udp", [DHCP_CLIENT_PORT], ["0.0.0.0/0"]),
    ]
    if cfg.tailscale_enabled:
        docs.append(_network_rule("tailscale", "udp", [TAILSCALE_PORT], ["0.0.0.0/0"]))
    if peers:
        docs.append(_network_rule("kubespan", "udp", [KUBESPAN_PORT], peers))
    for port in cfg.open_ports():
        docs.append(_network_rule(f"open-tcp-{port}", "tcp", [port], ["0.0.0.0/0"]))
    # talosctl merges NetworkRuleConfig documents by name: the `security-`
    # prefix keeps a user rule off the built-in names above, and two rules
    # whose names normalise alike would collapse into one document and drop a
    # port, so they are refused instead.
    rendered: dict[str, str] = {}
    for rule in cfg.security.values():
        if not rule.hosts:
            continue  # a rule without hosts closes its port; block does that
        name = re.sub(r"[^a-z0-9-]+", "-", rule.name.lower()).strip("-")
        if name in rendered:
            raise ConfigError(
                f"security rules {rendered[name]!r} and {rule.name!r} both render "
                f"as the Talos firewall rule 'security-{name}'; talosctl merges "
                "firewall rules by name, so rename one of them"
            )
        rendered[name] = rule.name
        docs.append(
            _network_rule(f"security-{name}", "tcp", [rule.port], list(rule.hosts.values()))
        )
    return docs


def _tailscale_patch(m: Machine, cfg: Config, auth_key: str) -> dict:
    extra_args = f"--login-server={cfg.login_server}" if cfg.login_server else ""
    return {
        "apiVersion": "v1alpha1",
        "kind": "ExtensionServiceConfig",
        "name": "tailscale",
        "environment": [
            f"TS_AUTHKEY={auth_key}",
            f"TS_HOSTNAME={m.name}",
            f"TS_EXTRA_ARGS={extra_args}",
        ],
    }


# WireGuard overhead KubeSpan subtracts from the path MTU.
KUBESPAN_MTU_OVERHEAD = 80


def _kubespan_patch(cfg: Config, mtu: int | None = None) -> dict:
    """machine.network.kubespan when `talos.kubespan` is on (opt-in).

    The WireGuard MTU is the path MTU minus the WireGuard overhead: the node
    L2's MTU (`mtu` overrides the cluster L2's, for a node sitting on a
    different one) while the cluster shares one L2, and the routed default
    once the peers span more than one -- cross-L2 packets leave through the
    gateway route clamped to 1500, and the tunnel follows the smaller path.
    Endpoint discovery excludes a configured `network.external` and, with the
    tailscale extension on, the tailscale tailnet range -- a tailnet address
    as a peer endpoint would tunnel WireGuard in WireGuard: Talos applies
    `filters.endpoints` as an allow-list, where a positive CIDR advertises a
    match and `!cidr` removes one, so the filter allows every address a node
    owns (`0.0.0.0/0`) and removes those -- the cluster-L2 address stays
    advertised.
    """
    l2 = mtu if mtu is not None else cfg.network.cluster.mtu
    base = DEFAULT_MTU if len(cfg.intra_cluster_cidrs()) > 1 else l2
    kubespan: dict = {
        "enabled": True,
        "mtu": base - KUBESPAN_MTU_OVERHEAD,
    }
    excluded: list[str] = []
    if cfg.tailscale_enabled:
        excluded.append(f"!{talosctl.TAILSCALE_NET}")
    if cfg.network.external is not None:
        external = cfg.network.external
        excluded.append("!" + external.cidr)
        if external.anchor_cidr:
            excluded.append("!" + external.anchor_cidr)
    if excluded:
        kubespan["filters"] = {"endpoints": ["0.0.0.0/0", *excluded]}
    return {"machine": {"network": {"kubespan": kubespan}}}


# A patch name becomes a filename, so it may only be a plain identifier: a
# provider is third-party code and must not be able to steer writes out of the
# temporary workdir.
_PATCH_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _patch_stem(host: str, name: str) -> str:
    if not isinstance(name, str) or not _PATCH_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"provider Talos patch name {name!r} for {host} must be lowercase "
            "letters, numbers and internal hyphens"
        )
    return f"{host}-{name}"


def _retag_kube_proxy(doc, tag: str):
    """Rewrite the kube-proxy image tag in any static-pod document to `tag`.

    The return-path pod is the one configuration part that bakes a kubernetes
    component image directly (kube-proxy as the vehicle that ships the `nft`
    binary), so `build_configs`' running-version override must reach it too or
    on an upgrade every node pulls the target kube-proxy at apply time, before
    the minor-by-minor upgrade. Accepts a single document dict or a list of
    Talos resource documents; mutates `doc` in place.
    """
    documents = doc if isinstance(doc, list) else [doc]
    for document in documents:
        if not isinstance(document, dict):
            continue
        pods = (document.get("machine") or {}).get("pods")
        if not isinstance(pods, list):
            continue
        for pod in pods:
            containers = (pod.get("spec") or {}).get("containers")
            if not isinstance(containers, list):
                continue
            for container in containers:
                image = container.get("image", "")
                if image.startswith("registry.k8s.io/kube-proxy:"):
                    container["image"] = f"registry.k8s.io/kube-proxy:{tag}"


def _write(workdir: Path, stem: str, doc) -> Path:
    """Dump a patch dict, a list of Talos documents, or raw YAML to its own file."""
    path = workdir / f"{stem}.yaml"
    if isinstance(doc, str):
        path.write_text(doc)
    elif isinstance(doc, list):
        path.write_text(yaml.safe_dump_all(doc, sort_keys=False, default_flow_style=False))
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return path


def build_configs(
    cfg: Config,
    machines: dict[str, Machine],
    endpoint: Endpoint,
    secrets_path: Path,
    installer_images: dict[tuple[str, ...], str],
    contributions: dict[str, TalosContribution],
    default_tags: dict[str, str] | None = None,
    kubernetes_version: str | None = None,
    talos_versions: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return {hostname -> machine-config YAML string} for every machine.

    `contributions` carries one provider contribution per hostname; the shared
    patches are written first, the provider's next, and the user's freeform
    patches last. `kubernetes_version` overrides `cfg.kubernetes_version` for
    the component images baked into the config (kubelet, kube-apiserver, ...):
    converge passes the version a running cluster is on so the upgrade goes
    through `talosctl upgrade-k8s` instead of a config push. It also retags the
    provider's return-path static pod (whose kube-proxy image is baked with the
    target) to the same running version so an upgrade never pulls the target
    kube-proxy before its minor-by-minor step. `talos_versions` overrides
    `cfg.talos_version` per hostname the same way -- the version a node RUNS
    decides the document layout its config is generated in, since converge
    applies configs before the Talos upgrade phase; a node absent from the map
    (a fresh cluster, a scale-up, a metal join) gets the target version.
    """
    cluster_endpoint = f"https://{endpoint.advertised_address}:6443"
    configs: dict[str, str] = {}
    missing = sorted(set(machines) - set(contributions))
    if missing:
        raise ConfigError(
            "no provider Talos contribution for: " + ", ".join(missing)
        )
    # the LUKS2 passphrase comes from the cluster's machine secrets, so a
    # cluster created before system disk encryption keeps generating configs
    # without the encryption settings
    passphrase = disk_passphrase(secrets_path)

    with tempfile.TemporaryDirectory(prefix="taloscluster-mc-") as tmp:
        workdir = Path(tmp)
        for host, m in machines.items():
            installer_image = installer_images[m.extensions]
            contribution = contributions[host]
            talos_version = (talos_versions or {}).get(host) or cfg.talos_version
            patches: list[Path] = [
                _write(workdir, f"{host}-machine",
                       _machine_patch(m, cfg, contribution.install_disk,
                                      default_tags, talos_version=talos_version)),
                _write(workdir, f"{host}-hostname", _hostname_patch(m)),
            ]
            if passphrase:
                patches.append(
                    _write(workdir, f"{host}-encryption",
                           _disk_encryption_patch(passphrase))
                )
            if m.role == "controlplane":
                patches.append(
                    _write(workdir, f"{host}-cluster",
                           _cluster_patch(cfg, talos_version=talos_version))
                )
            patches.append(_write(workdir, f"{host}-firewall", _firewall_docs(cfg)))
            if cfg.kubespan:
                patches.append(
                    _write(workdir, f"{host}-kubespan", _kubespan_patch(cfg))
                )
            auth_key = cfg.tailscale_auth_key
            if auth_key and "siderolabs/tailscale" in m.extensions:
                patches.append(
                    _write(workdir, f"{host}-tailscale",
                           _tailscale_patch(m, cfg, auth_key))
                )
            # provider contributions before the user's, so an explicit user
            # patch still has the last word
            for patch in contribution.patches:
                document = patch.document
                if kubernetes_version and kubernetes_version != cfg.kubernetes_version:
                    document = copy.deepcopy(document)
                    _retag_kube_proxy(document, kubernetes_version)
                patches.append(
                    _write(workdir, _patch_stem(host, patch.name), document)
                )
            # freeform user patches last so they can override
            for i, raw in enumerate(m.config_patches):
                patches.append(_write(workdir, f"{host}-extra-{i}", raw))

            configs[host] = talosctl.gen_config(
                cluster=cfg.name,
                endpoint=cluster_endpoint,
                secrets_path=secrets_path,
                output_type="controlplane" if m.role == "controlplane" else "worker",
                install_image=installer_image,
                install_disk=contribution.install_disk,
                kubernetes_version=kubernetes_version or cfg.kubernetes_version,
                talos_version=talos_version,
                patches=patches,
                additional_sans=[endpoint.advertised_address],
            )
    return configs
