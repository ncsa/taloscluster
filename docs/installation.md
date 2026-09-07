# Installation

Install [uv](https://docs.astral.sh/uv/), and put `talosctl` and `kubectl` on PATH. The package requires Python 3.10 or newer; uv can manage the Python environment. Provider credentials and network access are covered in the [Quickstart](quickstart.md).

For Proxmox, also provide an ISO-building utility on the management machine: `xorriso`, `genisoimage`, or macOS `hdiutil`. taloscluster uses it to build the temporary cloud-init ISO.

## Install and update

```bash
uv tool install git+https://github.com/ncsa/taloscluster   # install
uv tool upgrade taloscluster                                # update
```

Add the optional [plugins](concepts/plugins.md) with an extra — `[argocd]`, `[rancher]` or `[all]`:

```bash
uv tool install "taloscluster[all] @ git+https://github.com/ncsa/taloscluster"
```

Or run it without installing:

```bash
uvx --from git+https://github.com/ncsa/taloscluster taloscluster --help
```

From a checkout of this repo: `uv run taloscluster --help`.

## Development installation

Use the repository's uv environment to run the working tree and both bundled plugins:

```bash
git clone https://github.com/ncsa/taloscluster
cd taloscluster
uv sync --extra dev
uv run taloscluster --help
uv run pytest
```

The `dev` extra includes both plugins and the documentation and test tools. Run `uv sync --extra dev` again after changing dependencies or entry points. A separate `uv tool` installation uses its own environment; use `uv run taloscluster` here to exercise this checkout.

To remove a separate tool installation, run `uv tool uninstall taloscluster`; its installed plugins are removed with that tool environment.

## Verify the installation

```bash
taloscluster --version
taloscluster --help
talosctl version --client
kubectl version --client
```

Continue with the [Quickstart](quickstart.md) to create a cluster.
