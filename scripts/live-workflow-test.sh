#!/usr/bin/env bash
# Live workflow test for taloscluster.
#
# Exercises the documented quickstart, upgrade, drain and teardown workflows on
# a disposable OpenStack or Proxmox cluster and asserts each outcome. The unit
# suite in tests/ drives fake provider responses; this script is the only path
# that runs the real `taloscluster` CLI against a live provider, so a release
# should run it against a throwaway tenant before publication.
#
# Destructive by design: at the end it destroys the cluster and deletes the
# local client files. Only point it at a cluster created for this run -- the
# worker pool is scaled down and back up, and the pinned versions are bumped.
# See docs/testing.md for the full procedure.
#
# Usage:
#   scripts/live-workflow-test.sh --provider openstack -C /tmp/tt-mycluster
#   scripts/live-workflow-test.sh --provider proxmox -C /tmp/tt-mycluster \
#       --talos-version v1.37.2 --kubernetes-version v1.38.1
#
# Environment:
#   TALOSCLUSTER  how to run taloscluster (default: `uv run taloscluster`)
#   KUBECTL       kubectl binary (default: kubectl)
#   WAIT_SECS     seconds to wait for nodes to reach Ready (default 600)
#
# The cluster directory must already hold a working cluster.yaml + secrets.yaml:
# the harness does not invent provider credentials. Fill them with real values
# from the quickstart first. The harness applies two edits itself: it bumps the
# pinned versions for the upgrade phase, and it scales the largest worker pool
# down by one and back up for the drain phase.

set -euo pipefail

PROVIDER=""
DIR=""
TALOS_VERSION=""
KUBERNETES_VERSION=""
KUBECTL="${KUBECTL:-kubectl}"
WAIT_SECS="${WAIT_SECS:-600}"

if [[ -n "${TALOSCLUSTER:-}" ]]; then
    # The caller's value may be a command with arguments (e.g. `uv tool run
    # taloscluster`); split it into words, as docs/testing.md documents.
    read -r -a TC <<<"$TALOSCLUSTER"
else
    TC=(uv run taloscluster)
fi

script_usage() {
    sed -n '2,29p' "$0"
    echo
    echo "  --provider openstack|proxmox  provider to test against (required)"
    echo "  -C, --dir DIR                  cluster directory (required)"
    echo "  --talos-version VERSION        bump talos to this in the upgrade phase"
    echo "  --kubernetes-version VERSION   bump kubernetes to this in the upgrade phase"
    echo "  -h, --help                     show this help"
    exit "${1:-0}"
}

die() { echo "FAIL: $*" >&2; exit 1; }
info() { printf '\n=== %s ===\n' "$*"; }
tc() { "${TC[@]}" "$@"; }

# ---- YAML helpers (run under the repo's uv environment for PyYAML). --------
# yaml_set <file> <dotted.key> <value> — levels are written with their natural
# type: an all-digit value (a pool `count`) stays an integer, a version string
# stays a string, matching the cluster.yaml schema.
yaml_set() {
    local file="$1" key="$2" value="$3"
    uv run python - "$file" "$key" "$value" <<'PY'
import sys, yaml
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    value = int(value)
except (ValueError, TypeError):
    pass
doc = yaml.safe_load(open(path)) or {}
node = doc
parts = key.split(".")
for part in parts[:-1]:
    node = node.setdefault(part, {})
node[parts[-1]] = value
with open(path, "w") as f:
    yaml.safe_dump(doc, f, sort_keys=False)
PY
}

# yaml_get <file> <dotted.key> -> the scalar value, or empty when absent.
yaml_get() {
    local file="$1" key="$2"
    uv run python - "$file" "$key" <<'PY'
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
node = yaml.safe_load(open(path)) or {}
for part in key.split("."):
    if not isinstance(node, dict) or part not in node:
        sys.exit(0)
    node = node[part]
print(node)
PY
}

# The worker pool to drain: the configured pool with the highest count, so a
# zero-node test pool is never chosen.
pick_worker_pool() {
    local file="$1"
    uv run python - "$file" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1])) or {}
pools = doc.get("workers", {}) or {}
def count(k):
    c = pools[k].get("count", 0)
    return c if isinstance(c, int) else 0
if not pools:
    sys.exit("cluster.yaml has no worker pools; add one to exercise the drain phase")
print(max(pools, key=count))
PY
}

# ---- total desired nodes = control planes + every worker pool count. -------
desired_node_count() {
    local file="$1"
    uv run python - "$file" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1])) or {}
total = int((doc.get("controlplane", {}) or {}).get("count", 0) or 0)
for pool in (doc.get("workers", {}) or {}).values():
    c = pool.get("count", 0)
    total += int(c) if isinstance(c, int) else 0
print(total)
PY
}

# ---- wait for N nodes to all report Ready. ----
wait_ready_nodes() {
    # wait_ready_nodes <expected-count>
    local expected="$1" elapsed=0 step=10 ready total
    while (( elapsed < WAIT_SECS )); do
        ready="$("$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes \
            -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' \
            2>/dev/null | grep -c '^True' || true)"
        total="$("$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ' || true)"
        if [[ "${total:-0}" == "$expected" && "${ready:-0}" == "$expected" ]]; then
            return 0
        fi
        sleep "$step"
        elapsed=$((elapsed + step))
    done
    die "expected $expected Ready nodes, saw ${ready:-0}/$expected ($elapsed s)"
}

# ---- every node's kubelet must report the pinned Kubernetes minor, mirroring
# the `check` version comparison (numeric leading pair, v-prefix-insensitive). ---
assert_on_kubernetes_minor() {
    # assert_on_kubernetes_minor <k8s-target-version>
    local target="$1"
    "$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes \
        -o jsonpath='{range .items[*]}{.status.nodeInfo.kubeletVersion}{"\n"}{end}' \
        2>/dev/null | uv run python -c '
import sys
target = sys.argv[1]
def minor(v):
    import re
    m = re.match(r"^v?(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else None
tm = minor(target)
lines = []
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    lines.append(line)
if not lines:
    sys.exit("no nodes returned a kubelet version to verify")
bad = [v for v in lines if minor(v) != tm]
if bad:
    print("kubelet versions off the pinned minor:", file=sys.stderr)
    for v in sorted(set(bad)):
        print("  " + v, file=sys.stderr)
    sys.exit(1)
print("all kubelets on kubernetes", target)
' "$target"
}

# ---------------------------------------------------------------- options ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider) [[ $# -ge 2 ]] || script_usage 1; PROVIDER="$2"; shift 2 ;;
        -C|--dir) [[ $# -ge 2 ]] || script_usage 1; DIR="$2"; shift 2 ;;
        --talos-version) [[ $# -ge 2 ]] || script_usage 1; TALOS_VERSION="$2"; shift 2 ;;
        --kubernetes-version) [[ $# -ge 2 ]] || script_usage 1; KUBERNETES_VERSION="$2"; shift 2 ;;
        -h|--help) script_usage 0 ;;
        *) echo "unknown option: $1" >&2; script_usage 1 ;;
    esac
done

[[ -n "$PROVIDER" ]] || { echo "missing --provider" >&2; script_usage 1; }
case "$PROVIDER" in openstack|proxmox) ;; *) die "unsupported provider: $PROVIDER" ;; esac
[[ -n "$DIR" ]] || { echo "missing --dir" >&2; script_usage 1; }
[[ -d "$DIR" ]] || die "cluster directory does not exist: $DIR"
[[ -f "$DIR/cluster.yaml" ]] || die "no cluster.yaml in $DIR; run init and fill it first"
[[ -f "$DIR/secrets.yaml" ]] || die "no secrets.yaml in $DIR; run init and fill it first"

command -v "$KUBECTL" >/dev/null 2>&1 || die "required command not found: kubectl"

# ------------------------------------------------------------- preflight ----
info "preflight ($PROVIDER)"
tc --version >/dev/null || die "taloscluster did not run"
if ! tc -C "$DIR" status -o yaml >/dev/null 2>&1; then
    echo "provider credential did not authenticate (run eval \"\$(taloscluster env)\" to debug)" >&2
    exit 1
fi

CLUSTER_FILE="$DIR/cluster.yaml"

# ------------------------------------------------- quickstart: plan/apply ----
info "quickstart: plan (dry-run changes nothing)"
tc -C "$DIR" plan

info "quickstart: converge"
tc -C "$DIR" converge

info "quickstart: verifying every node is Ready after bootstrap"
"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes -o wide
wait_ready_nodes "$(desired_node_count "$CLUSTER_FILE")"

# --------------------------------------------------- upgrade: bump versions --
info "upgrade: choosing target versions"
if [[ -z "$TALOS_VERSION" || -z "$KUBERNETES_VERSION" ]]; then
    # The cloud-free `check -o yaml` report names the newest patch of each pinned
    # minor, the in-place bump the docs recommend for a quiet test upgrade.
    check_yaml="$(tc -C "$DIR" check -o yaml 2>/dev/null || true)"
    if [[ -z "$TALOS_VERSION" ]]; then
        TALOS_VERSION="$(printf '%s' "$check_yaml" | uv run python -c \
'import sys,yaml
d=yaml.safe_load(sys.stdin.read()) or {}
print(next((c["latest_patch"] for c in d.get("components", [])
            if c["component"]=="talos" and c.get("latest_patch")), ""))' 2>/dev/null || true)"
    fi
    if [[ -z "$KUBERNETES_VERSION" ]]; then
        KUBERNETES_VERSION="$(printf '%s' "$check_yaml" | uv run python -c \
'import sys,yaml
d=yaml.safe_load(sys.stdin.read()) or {}
print(next((c["latest_patch"] for c in d.get("components", [])
            if c["component"]=="kubernetes" and c.get("latest_patch")), ""))' 2>/dev/null || true)"
    fi
fi
[[ -n "$TALOS_VERSION" ]] || die "no upgrade target for talos (pass --talos-version, or check must reach upstream)"
[[ -n "$KUBERNETES_VERSION" ]] || die "no upgrade target for kubernetes (pass --kubernetes-version, or check must reach upstream)"
# A bump that equals the pinned version would converge to a no-op and "pass"
# without exercising an upgrade; refuse so a run always restarts the nodes.
old_talos="$(yaml_get "$CLUSTER_FILE" talos.version)"
old_k8s="$(yaml_get "$CLUSTER_FILE" kubernetes.version)"
[[ "$TALOS_VERSION" != "$old_talos" ]] || \
    die "talos $TALOS_VERSION already matches the pin; pass --talos-version to force an upgrade"
[[ "$KUBERNETES_VERSION" != "$old_k8s" ]] || \
    die "kubernetes $KUBERNETES_VERSION already matches the pin; pass --kubernetes-version to force an upgrade"

info "upgrade: talos $TALOS_VERSION, kubernetes $KUBERNETES_VERSION"
yaml_set "$CLUSTER_FILE" talos.version "$TALOS_VERSION"
yaml_set "$CLUSTER_FILE" kubernetes.version "$KUBERNETES_VERSION"
tc -C "$DIR" plan
tc -C "$DIR" converge

info "upgrade: asserting every node reports the target versions"
"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes -o wide
"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes \
    -o jsonpath='{range .items[*]}{.metadata.name}{" kubelet="}{.status.nodeInfo.kubeletVersion}{"\n"}{end}'
assert_on_kubernetes_minor "$KUBERNETES_VERSION"

# ------------------------------------------------------ drain: scale down/up --
info "drain: locating the worker pool to scale"
WORKER_POOL="$(pick_worker_pool "$CLUSTER_FILE")" \
    || die "no usable worker pool in $CLUSTER_FILE"
old_count="$(yaml_get "$CLUSTER_FILE" "workers.$WORKER_POOL.count")"
[[ -n "$old_count" ]] || die "worker pool '$WORKER_POOL' has no count"
new_count=$((old_count - 1))
if (( new_count < 1 )); then
    die "worker pool '$WORKER_POOL' has count $old_count; raise it to at least 2 first"
fi

info "drain: scaling '$WORKER_POOL' $old_count -> $new_count"
yaml_set "$CLUSTER_FILE" "workers.$WORKER_POOL.count" "$new_count"
tc -C "$DIR" plan
# --yes approves the drained node's deletion (the docs use a confirmation otherwise)
tc -C "$DIR" converge --yes

info "drain: asserting the scaled-down node is gone and the rest are Ready"
"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes -o wide
wait_ready_nodes "$(desired_node_count "$CLUSTER_FILE")"

info "drain: scaling '$WORKER_POOL' back up to $old_count"
yaml_set "$CLUSTER_FILE" "workers.$WORKER_POOL.count" "$old_count"
tc -C "$DIR" converge
"$KUBECTL" --kubeconfig "$DIR/kubeconfig" get nodes -o wide
wait_ready_nodes "$(desired_node_count "$CLUSTER_FILE")"

# -------------------------------------------------------------- teardown ----
info "teardown: destroy (dry-run, then destroy)"
tc -C "$DIR" destroy --dry-run
tc -C "$DIR" destroy --yes

info "teardown: asserting the local client files are gone"
for f in kubeconfig talosconfig talossecrets.yaml; do
    [[ -f "$DIR/$f" ]] && die "destroy left $f behind"
done

info "teardown: done"
echo "live workflow test PASSED ($PROVIDER, $DIR)"
