# Live workflow testing

The unit suite in `tests/` exchanges real provider and cluster responses for fakes, and the documentation build checks that the docs parse and load. Neither proves the documented workflows work against a real provider. This page is the counterpart: a repeatable way to exercise the quickstart, upgrade, drain, and teardown workflows on disposable OpenStack and Proxmox clusters, so a release is verified against live provider behavior before it is published.

The procedure uses `scripts/live-workflow-test.sh`, which drives the real `taloscluster` CLI end to end and asserts each phase's outcome. Everything it does is destructive to the cluster directory you point it at, so it is meant for a throwaway tenant and a cluster created for the run.

## When to run it

Run the full lifecycle before cutting a release, after a change that touches the provider backends (`taloscluster/openstack/`, `taloscluster/proxmox/`), the Talos machine config, or the converge pipeline ([`converge`](concepts/lifecycle.md)). A single passing run on one provider does not cover the other: [Talos and the providers](concepts/machines.md) differ, so exercise both OpenStack and Proxmox before release.

## Prerequisites

- A disposable OpenStack project or Proxmox cluster with enough capacity for three nodes (one control plane and two workers, as the example below lays out), an application credential (OpenStack) or API token (Proxmox) that can create and delete the resources the quickstart needs, and access to the Talos Image Factory and `dl.k8s.io` for the upgrade phase. Follow the [OpenStack setup](providers/openstack.md) or [Proxmox setup](providers/proxmox.md) guide for the tenant, quotas, and credentials.
- `kubectl` and `talosctl` on `PATH` (taloscluster shells out to them), with the node addresses reachable from this machine — on a tailnet or through a direct route, per [Machines and access](concepts/machines.md#reaching-the-nodes).
- A cluster directory with a working `cluster.yaml` and `secrets.yaml`, exactly as prepared in the [quickstart](quickstart.md). The harness does not invent provider credentials; it reads your real values. Plan for the drain phase: it scales a worker pool down by one and back up, so configure at least two workers.

## Initialize and configure a disposable cluster

Pick one provider, initialize the directory, and fill the configuration with your disposable tenant's values:

```bash
taloscluster init --openstack -C /tmp/tt-mycluster mycluster
# Or: taloscluster init --proxmox -C /tmp/tt-mycluster mycluster
cd /tmp/tt-mycluster
vi cluster.yaml secrets.yaml
```

`init` scaffolds a complete, valid `cluster.yaml`; edit only the keys below with your disposable tenant's values. Only `name:` and the mandatory `network:` block stay as scaffolded — the network CIDR, DNS, and NTP it already contains are fine unless your tenant needs different ones. The provider section also comes from the scaffold but holds placeholders that must be replaced with your tenant's values: OpenStack's `url`, `availability_zone`, and `external_net`, or Proxmox's `url`, `storage`, `iso_storage`, and `cidata_storage`, per the [OpenStack setup](providers/openstack.md) or [Proxmox setup](providers/proxmox.md) guide. A literal scaffold leaves the provider pointing at `example.edu` and the run dies in preflight; see the configuration reference for the full key list.

```yaml
# cluster.yaml — the keys to change from the scaffold (tailscale omitted for direct access)
talos:
  version: v1.13.8
kubernetes:
  version: v1.36.1
controlplane:
  count: 1        # a single control plane is fine for a test cluster
  # OpenStack:  flavor: gp.medium
  # Proxmox:    cores: 4   memory: 8   # GB
  disk: 40
workers:
  worker:
    count: 2      # at least two, so the drain phase can scale one away
    # OpenStack:  flavor: gp.xlarge
    # Proxmox:    cores: 8   memory: 16  # GB
    disk: 100
security:
  kubernetes:
    office: 203.0.113.0/24      # your management network
  talos:
    office: 203.0.113.0/24
```

```yaml
# secrets.yaml — real disposable-tenant credentials
# OpenStack:
openstack:
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
# Proxmox:
# proxmox:
#   token_id: "taloscluster@pve!provider"
#   token_secret: "CHANGE-ME"
```

Replace the example allowlist and credential placeholders with your tenant's values; the release-specific versions above are illustrative, not pinned. The example comments out the second provider only to keep one block readable — use the single provider you initialized.

**Tailscale:** the scaffold adds a `tailscale:` block to both files — `cluster.yaml` sets `login_server` and `secrets.yaml` carries an `auth_key: "CHANGE-ME"` placeholder. Both must be handled before converge: (a) for the direct-access mode these examples describe, remove the `tailscale:` section from `cluster.yaml` — a section only `secrets.yaml` carries leaves Tailscale off, but a leftover `auth_key: "CHANGE-ME"` is refused as soon as converge uses it (before any machine config is applied), and a leftover `cluster.yaml` section with no auth key still flips Tailscale on and converge dials the unresolvable `<cluster>-controlplane-01` name and hangs until it times out; or (b) supply your real headscale `login_server` and a pre-auth key. If your management machine reaches the nodes over the tailnet, keep the `tailscale: 100.64.0.0/10` entries in the security allowlists (the examples show `office:` in their place, which would lock out tailnet access).

## Run the lifecycle

From the repository root, with `uv` available:

```bash
uv run scripts/live-workflow-test.sh --provider openstack -C /tmp/tt-mycluster
```

For Proxmox, pass `--provider proxmox`. The harness runs four phases and stops with a non-zero exit at the first failed assertion:

1. **Quickstart** — `plan`, then `converge`, then waits for the configured control planes and workers to report `Ready`, exercising [Plan and converge](commands.md#converge) as written.
2. **Upgrade** — bumps the pinned `talos.version` and `kubernetes.version` to the argument you pass (`--talos-version`, `--kubernetes-version`) or, when omitted, to the newest patch release of each pinned minor that [`check`](commands.md#check) proposes, then `converge` again. A target that already equals its pin is refused (pass the flags with a newer version), so the phase cannot silently no-op. `converge` verifies each node reaches the target Talos version and refuses to move on until it does, so a clean exit after the bump proves the Talos rollout landed; the harness then independently asserts every node's kubelet reports the pinned Kubernetes minor, reading the versions from the cluster's `kubeconfig`. See [Upgrade](usage.md#upgrade-talos-or-kubernetes).
3. **Drain** — scales the largest worker pool down by one and `converge` with `--yes`, asserting the drained node is gone while the rest stay `Ready`, then scales the pool back up and asserts the cluster returns to full strength. This is the scale-down drain, reset, and delete path behind [Maintenance](maintenance.md).
4. **Teardown** — `destroy --dry-run`, then `destroy --yes`, and asserts the local `kubeconfig`, `talosconfig`, and `talossecrets.yaml` are removed. See [Tear down](usage.md#tear-down).

The harness writes the new versions and the pool count into `cluster.yaml` itself, so confirm the diff in `plan` at each phase rather than editing by hand. On a run you leave for later, re-check the pool count and versions match what you expect before continuing, because the harness resets neither on re-entry.

## What a passing run shows

A run that reaches `live workflow test PASSED` proves the provider can create the resources, Talos can boot them, Kubernetes comes up healthy, a version bump rolls out without losing a node, a scale-down drains and removes a node, a scale-up recreates one, and `destroy` tears it all back down and leaves the directory clean. The assertions are node counts and `Ready` status from the cluster's own `kubeconfig`, so they fail loudly rather than trusting a single command's exit code.

## Running it as a release gate

Because it needs live credentials and threads on provider capacity, the live workflow test is not part of the automated `tests` workflow. Run it locally against both providers and one disposable cluster each before tagging a release, alongside `uv run pytest` and `uv run mkdocs build --strict`, as the repository's release rules require. The harness runs taloscluster through `uv run` by default; set `TALOSCLUSTER`, for example to a `uv tool run` binary, to smoke-test a published wheel instead:

```bash
TALOSCLUSTER="uv tool run taloscluster" uv run scripts/live-workflow-test.sh \
    --provider openstack -C /tmp/tt-mycluster
```

Clean up the disposable tenant after teardown completes and the tailnet membership the nodes registered. The shared boot image is retained by design; remove it with [`taloscluster image remove`](commands.md#image) if you do not need it for the next test cluster.
