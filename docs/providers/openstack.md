# OpenStack setup

Prepare the provider before following the [Quickstart](../quickstart.md). For each configuration key, see the [OpenStack reference](../configuration/openstack.md).

## Tenant and project

Run `taloscluster` as a user in the project where the cluster will live. The application credential is scoped to a project, and that project's name becomes the `ncsa/project` node label; converge never reads another project's tenant. Give the project enough quota for everything the cluster owns (see [Service quotas](#service-quotas) below).

## Application credentials

Create an application credential with `python-openstackclient` (the `openstack` CLI, available via `uv sync --extra openstack`):

```bash
openstack --os-project-name myproject application credential create taloscluster
```

Copy the output id into `openstack.credential_id` and the secret into `openstack.credential_secret` in `secrets.yaml`, replacing the scaffolded `CHANGE-ME` values:

```yaml
# secrets.yaml
openstack:
  credential_id: "CHANGE-ME"
  credential_secret: "CHANGE-ME"
```

taloscluster authenticates to the [Keystone identity endpoint](../configuration/openstack.md#openstackurl) with the `v3applicationcredential` auth type (`OS_AUTH_TYPE=v3applicationcredential`). It refuses a null, empty, or still-scaffolded credential at secrets load time instead of failing later as an opaque 401. Create one credential per cluster (or reuse a name); the credential's project is the only project the session can see.

## Required services

The provider touches five services during a converge. All must be present and reachable in the configured `region` (`RegionOne` by default):

- **Keystone** (identity) — the `openstack.url` endpoint and the session scoping, including the `region` and the project label.
- **Nova** (compute) — flavors and servers; each server is created volume-backed.
- **Neutron** (network) — the tenant network, subnet, router, ports, security group and floating IPs.
- **Glance** (image) — the shared Talos boot image, built and uploaded once per `talos.version`. The image needs a minimum 20 GB disk and 2 GiB RAM (`min_disk=20`, `min_ram=2048`) and the `hw_qemu_guest_agent=yes` property so the baked QEMU guest agent attaches its virtio-serial channel.
- **Cinder** (volume) — the boot volumes behind each server; converge reads a server's boot volume size through Cinder to detect a `disk` change.

Out-of-region or absent services cause a fail-fast error at the phase that needs them. `openstack region list` shows the available regions; if your cloud's region is not the default (`RegionOne`), copy your region's name into `openstack.region` (see [The configured region](#the-configured-region)).

## Service quotas

A single cluster creates the following project-owned resources, so size the project quota accordingly (multiply per node for the per-server items):

| Resource | Count | Notes |
| --- | --- | --- |
| Server (VM) | nodes | one per machine |
| Boot volume | nodes | volume-backed, `delete_on_termination` |
| Port | nodes + 2 | one per machine plus one each for the API and ingress VIPs |
| Floating IP | 2 | one for the Kubernetes API, one for ingress |
| Network | 1 | the tenant network from `network.cidr` |
| Subnet | 1 | the subnet underlying `network.cidr` |
| Router | 1 | attached to the external network |
| Security group | 1 | named for the cluster |
| Image | 1 | shared across all nodes (and potentially clusters) on the same `talos.version` |

The two floating IPs come from the external network's pool. If the project's floating-IP quota is at its limit, converge fails at the network phase; free one or raise the quota. The boot-volume quota must cover `disk` (GB) for every node.

## The configured region

`RegionOne` is the default region: `openstack.region` is optional and may be omitted for the common case. The session connects in the configured region, and the same region is what `status`, the `env` command, and the ArgoCD cluster-apps values report. If your cloud serves more than one region, set `openstack.region` to the one hosting your project's resources (the endpoint and service catalog must agree).

## External-network selection

`openstack.external_net` names the provider (external) network the cluster router and the two floating IPs attach to. It must be a real external network with available floating-IP addresses and an upstream route; taloscluster does not create one for you. It looks the network up read-only by name and refuses to proceed if it is not found, so use the name exactly as it appears to your project (for example `ext-net`):

```yaml
openstack:
  url: https://openstack.example.edu:5000/v3/
  availability_zone: nova
  external_net: ext-net
  region: RegionOne
```

The router gets the external network as its external gateway; the subnet's nodes reach it through the router, and the API and ingress floating IPs are each associated with a reserved port on the private network. Servers themselves get no floating IP — only the router's gateway address and the two floating IPs are exposed.

## Availability zones and flavors

Pick an `availability_zone` (for example `nova`) with enough free capacity. Every server is created volume-backed in that zone from the shared boot image. The Glance image is region-scoped, so it lives in the same region as the servers — no public visibility is required.

Each pool names its `flavor` (see [Pools](../configuration/pools.md)); the flavor must exist and be public to the credential's project, with at least the image's `min_disk`/`min_ram`:

```yaml
controlplane:
  count: 3
  flavor: gp.medium
  disk: 40 # GB boot volume

workers:
  worker:
    count: 3
    flavor: gp.xlarge
    disk: 100
```

The configured `flavor` may be a name or an id. `disk` is the boot volume size in GB, which may exceed the flavor's own disk. Servers are create-only: converge refuses a later `flavor`, `disk`, or `availability_zone` change on an existing node (recreate it by scaling its pool down past it and back up) rather than silently ignoring it, and `plan` reports the same refusal.

## Management access

taloscluster reaches the Talos API on port 50000 of a real node address to bootstrap and manage the cluster, and clients reach the Kubernetes API on the floating IP on port 6443. On OpenStack the nodes sit on the private tenant network with no public address and no SSH, so you must get `taloscluster` to a node address from your management machine — usually through [Tailscale](../concepts/machines.md#reaching-the-nodes):

- Install and connect Tailscale on the machine you run `taloscluster` from so it is already on the same tailnet; taloscluster does not add it. Each node joins at boot using the `tailscale.auth_key` in `secrets.yaml`.
- On OpenStack without Tailscale, a bastion host or a VPN into the tenant network is your only path to a node; the tool falls back to the provider-reported address, which you must then reach.
- Include the management network and the tailnet in the [`security`](../configuration/security.md) `kubernetes` (tcp/6443) and `talos` (tcp/50000) allowlists, plus any `https`/`http` rules you restrict. The cluster security group carries these rules on every port, and the two VIP ports carry the cluster security group explicitly so the allowlists apply at the floating IPs too.

Converge allocates the Kubernetes API VIP and floating IP for you during the network phase; there is no `kubeapi_vip` key on OpenStack. Use `eval "$(taloscluster env)"` to export the connection environment (including the configured region) for troubleshooting with `openstack`; it prints the credential secret, so use it via `eval` rather than logging it.
