"""taloscluster command-line entrypoint.

    taloscluster init [NAME]                    # scaffold cluster.yaml / secrets.yaml / .gitignore
    taloscluster converge [--dry-run] [--yes]   # default; make the cluster match cluster.yaml
    taloscluster plan                           # dry-run converge: print what would change
    taloscluster status [-o yaml]               # show managed resources, endpoints + nodes
    taloscluster check [-o yaml]                # are talos/kubernetes up to date?
    taloscluster destroy [--yes]                # tear down all managed resources

`plan` is just `converge --dry-run`; both print every state-changing action they
would take. Any deletion in converge requires --yes (or an interactive confirm),
recovering the safety terraform's plan gave.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import converge as _converge
from . import scaffold as _scaffold
from .errors import ConfigError, PreflightError, ReconcileError, StateError
from .output import Die, set_dry_run


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-C", "--dir", default=".", metavar="DIR",
        help="cluster directory holding cluster.yaml / secrets.yaml / "
             "talossecrets.yaml (default: current directory)",
    )


def _cmd_init(args, root):
    _scaffold.init(root, name=args.name)


def _cmd_converge(args, root):
    set_dry_run(bool(args.dry_run))
    _converge.converge(root, assume_yes=args.yes)


def _cmd_plan(args, root):
    set_dry_run(True)
    _converge.converge(root, assume_yes=False)


def _cmd_status(args, root):
    _converge.status(root, output=args.output)


def _cmd_check(args, root):
    return _converge.check(root, output=args.output)


def _cmd_dashboard(args, root):
    _converge.dashboard(root, nodes=args.nodes)


def _cmd_env(args, root):
    _converge.print_env(root)


def _cmd_image(args, root):
    set_dry_run(bool(args.dry_run))
    if args.action == "download":
        _converge.image_download(root)
    else:
        _converge.image_remove(root, assume_yes=args.yes)


def _cmd_destroy(args, root):
    set_dry_run(bool(args.dry_run))
    _converge.destroy(root, assume_yes=args.yes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taloscluster",
        description="Converge a Talos Kubernetes cluster on OpenStack to cluster.yaml.",
        epilog=(
            "examples:\n"
            "  taloscluster init mycluster      scaffold cluster.yaml/secrets.yaml/.gitignore\n"
            "  taloscluster plan                dry-run: print every create/update/delete\n"
            "  taloscluster converge            make the cluster match cluster.yaml\n"
            "  taloscluster converge --yes      converge, approving deletions without a prompt\n"
            "  taloscluster status              list managed resources, endpoints and nodes\n"
            "  taloscluster check               compare pinned versions against the newest\n"
            "                                   releases\n"
            "  taloscluster destroy             delete all managed resources (image is kept)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"taloscluster {__version__}")
    sub = parser.add_subparsers()

    p_init = sub.add_parser(
        "init",
        help="scaffold cluster.yaml, secrets.yaml and .gitignore in a new directory",
        description="Write starter cluster.yaml and secrets.yaml templates plus a "
                    ".gitignore covering the secret/derived files (secrets.yaml, "
                    "talossecrets.yaml, talosconfig, kubeconfig). Never overwrites "
                    "an existing cluster.yaml or secrets.yaml; an existing "
                    ".gitignore only gets the entries it is missing. Edit both "
                    "files, then run `taloscluster plan`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_init)
    p_init.add_argument(
        "name", nargs="?", default="mycluster",
        help="cluster name written into cluster.yaml (default: mycluster)",
    )
    p_init.set_defaults(func=_cmd_init)

    p_con = sub.add_parser(
        "converge",
        help="converge the cluster to cluster.yaml",
        description=(
            "Make the cluster match cluster.yaml. Runs, in order: build image(s), "
            "ensure talos secrets, reconcile network + security group, discover "
            "existing nodes, scale down removed nodes (drain -> reset -> delete), "
            "upgrade existing nodes to the target talos/kubernetes versions, create "
            "new nodes, bootstrap (new cluster only), fetch kubeconfig, health-check. "
            "Idempotent: safe to re-run any time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_con)
    p_con.add_argument(
        "--dry-run", action="store_true",
        help="print every state-changing action without doing it (like DEBUG=echo)",
    )
    p_con.add_argument(
        "--yes", action="store_true",
        help="approve node/resource deletions without the interactive confirm prompt",
    )
    p_con.set_defaults(func=_cmd_converge)

    p_plan = sub.add_parser(
        "plan",
        help="dry-run converge (alias for converge --dry-run)",
        description="Dry-run a converge: print every create/update/delete that "
                    "converge would perform. Changes nothing in OpenStack or the "
                    "cluster (it does POST idempotent schematics to factory.talos.dev "
                    "and list OpenStack resources).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_plan)
    p_plan.set_defaults(func=_cmd_plan)

    p_status = sub.add_parser(
        "status",
        help="show managed resources and nodes",
        description="Print the OpenStack endpoint/region/project this cluster "
                    "lives in (never the credential), the resources tagged as "
                    "belonging to it (networks, subnets, routers, security group, "
                    "ports, floating ips, servers), the kube-api and ingress floating "
                    "ips (with their private VIPs) and, if reachable, "
                    "`kubectl get nodes`. `-o yaml` prints the same information "
                    "as a yaml document instead.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_status)
    p_status.add_argument(
        "-o", "--output", choices=["text", "yaml"], default="text",
        help="output format (default: text)",
    )
    p_status.set_defaults(func=_cmd_status)

    p_check = sub.add_parser(
        "check",
        help="check whether talos/kubernetes are up to date",
        description="Compare the versions pinned in cluster.yaml against the "
                    "newest upstream releases (factory.talos.dev for talos, "
                    "dl.k8s.io for kubernetes) and against what the cluster "
                    "actually runs. Reports the newest patch of the pinned "
                    "minor (a safe in-place bump) separately from the newest "
                    "release overall, plus any node not yet on the pinned "
                    "versions. Read-only: it needs no OpenStack credentials and "
                    "changes nothing. Exits 1 when an update or a drift was "
                    "found, 0 when everything is current, so it can gate CI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_check)
    p_check.add_argument(
        "-o", "--output", choices=["text", "yaml"], default="text",
        help="output format (default: text)",
    )
    p_check.set_defaults(func=_cmd_check)

    p_dash = sub.add_parser(
        "dashboard",
        help="open talosctl dashboard on all nodes",
        description="Open `talosctl dashboard` on every node of the cluster. "
                    "Nodes come from OpenStack (every managed machine) plus talos "
                    "cluster discovery (their talos-level addresses), so a node "
                    "that booted but never joined kubernetes is still included. "
                    "Nodes whose apid does not answer are reported and dropped -- "
                    "talosctl dashboard aborts if any single target is unreachable. "
                    "Requires this machine to be on the tailnet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_dash)
    p_dash.add_argument(
        "nodes", nargs="*", metavar="NODE",
        help="node address(es) to show; default is every node of the cluster",
    )
    p_dash.set_defaults(func=_cmd_dashboard)

    p_env = sub.add_parser(
        "env",
        help="print OS_* auth exports for the openstack CLI",
        description="Print `export OS_...` lines (from cluster.yaml + secrets.yaml) "
                    "so the openstack CLI uses the same application credential. "
                    "Usage: eval \"$(taloscluster env)\". Prints the credential "
                    "secret to stdout — intended for eval, not logging.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_env)
    p_env.set_defaults(func=_cmd_env)

    p_image = sub.add_parser(
        "image",
        help="download (build+upload) or remove the boot image",
        description="Manage the single Glance boot image (talos-<version>-tailscale, "
                    "baked with tailscale + qemu-guest-agent). `download` builds it "
                    "from the factory and uploads it if absent; `remove` deletes it "
                    "(converge never deletes it on its own).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_image)
    p_image.add_argument(
        "action", choices=["download", "remove"],
        help="download = build + upload to Glance if missing; remove = delete from Glance",
    )
    p_image.add_argument("--dry-run", action="store_true", help="print actions, change nothing")
    p_image.add_argument("--yes", action="store_true",
                         help="skip the confirm prompt on remove")
    p_image.set_defaults(func=_cmd_image)

    p_destroy = sub.add_parser(
        "destroy",
        help="delete all managed resources",
        description="Delete every taloscluster-managed OpenStack resource for this "
                    "cluster (servers, ports, floating ips, router, subnet, network, "
                    "security group). The shared boot image is NOT deleted. Prompts "
                    "for confirmation unless --yes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_destroy)
    p_destroy.add_argument(
        "--dry-run", action="store_true",
        help="print what would be deleted without deleting anything",
    )
    p_destroy.add_argument(
        "--yes", action="store_true",
        help="skip the 'type the cluster name to confirm' prompt",
    )
    p_destroy.set_defaults(func=_cmd_destroy)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    root = Path(args.dir).resolve()

    try:
        # commands return an exit code only when they have a verdict to report
        # (`check` exits 1 on an available update); the rest return None -> 0.
        rc = args.func(args, root)
    except Die as e:
        print(e, file=sys.stderr)
        return 1
    except (ConfigError, StateError, ReconcileError, PreflightError,
            RuntimeError, TimeoutError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"ERROR: command failed: {' '.join(str(a) for a in e.cmd)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
