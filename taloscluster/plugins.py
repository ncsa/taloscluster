"""Optional plugins: discovery, ordering and fan-out.

A plugin is a separately installed distribution (``taloscluster[argocd]``) that
advertises itself through a ``taloscluster.plugins`` entry point. Core never
imports a plugin by name -- whatever is installed is what runs -- so a new plugin
is a new folder under plugins/ and no edit here.

The plugin protocol is duck-typed against the entry point's module. Only
``configured`` and ``converge`` are required; the rest are skipped when absent:

    AFTER: tuple[str, ...]                      # run after these, if installed
    init(root: Path) -> None                    # add missing scaffold sections
    configured(ctx) -> bool
    converge(ctx, assume_yes=False) -> dict | None
    destroy(ctx, assume_yes=False) -> None
    status(ctx) -> dict
    check(ctx) -> dict                          # carries an "ok": bool

Failures are contained: a plugin that cannot be loaded is dropped with a warning,
and one that raises during a hook does not stop the others -- it only makes the
command's exit code non-zero. A converge that already built the cluster must not
be thrown away because a downstream registration failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from .context import Context
from .output import warn

ENTRY_POINT_GROUP = "taloscluster.plugins"
HOOKS = ("init", "converge", "destroy", "status", "check")


@dataclass(frozen=True)
class Plugin:
    name: str
    module: Any
    after: tuple[str, ...]

    def has(self, hook: str) -> bool:
        return callable(getattr(self.module, hook, None))

    def call(self, hook: str, ctx: Context, **kw) -> Any:
        return getattr(self.module, hook)(ctx, **kw)

    def configured(self, ctx: Context) -> bool:
        fn = getattr(self.module, "configured", None)
        return bool(fn(ctx)) if callable(fn) else False


def discover() -> list[Plugin]:
    """Every installed plugin, in the order they should run."""
    found: list[Plugin] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = ep.load()
        except Exception as e:  # a broken plugin must not take the CLI down
            warn(f"plugin {ep.name!r} could not be loaded ({e}); skipping")
            continue
        after = tuple(getattr(module, "AFTER", ()) or ())
        found.append(Plugin(name=ep.name, module=module, after=after))
    return _order(found)


def initialize(root: Path) -> None:
    """Let every installed plugin add its missing scaffold sections."""
    for plugin in discover():
        if not plugin.has("init"):
            continue
        try:
            plugin.module.init(root)
        except Exception as e:
            warn(f"plugin {plugin.name!r} failed during init: {e}")


def _order(plugins: list[Plugin]) -> list[Plugin]:
    """Topological sort over the AFTER edges, alphabetical within a tier.

    `AFTER` is a soft declaration -- a name that is not installed is ignored, so
    a plugin that would like to run after rancher still works on its own. Ties
    break alphabetically so the order never depends on entry-point iteration
    order. A cycle is reported and degrades to plain alphabetical rather than
    failing the command.
    """
    by_name = {p.name: p for p in plugins}
    # only edges to plugins that are actually installed
    pending = {p.name: {a for a in p.after if a in by_name} for p in plugins}

    ordered: list[Plugin] = []
    while pending:
        ready = sorted(name for name, deps in pending.items() if not deps)
        if not ready:
            warn("plugin ordering has a cycle "
                 f"({', '.join(sorted(pending))}); falling back to alphabetical")
            ordered.extend(by_name[n] for n in sorted(pending))
            break
        for name in ready:
            ordered.append(by_name[name])
            del pending[name]
        for deps in pending.values():
            deps.difference_update(ready)
    return ordered


def active(ctx: Context) -> list[Plugin]:
    """The installed plugins that this cluster directory actually configures."""
    out = []
    for p in discover():
        try:
            if p.configured(ctx):
                out.append(p)
        except Exception as e:
            warn(f"plugin {p.name!r}: could not tell whether it is configured ({e}); skipping")
    return out


def run(plugins: list[Plugin], hook: str, ctx: Context, **kw) -> int:
    """Call `hook` on each plugin that implements it. Returns 1 if any failed.

    A converge's return value is recorded in ctx.results[<name>] before the next
    plugin runs -- that is how a later plugin sees an earlier one's output.
    """
    failed = 0
    for p in plugins:
        if not p.has(hook):
            continue
        try:
            result = p.call(hook, ctx, **kw)
        except Exception as e:
            warn(f"plugin {p.name!r} failed during {hook}: {e}")
            failed = 1
            continue
        if isinstance(result, dict):
            ctx.results[p.name] = result
    return failed


def collect(plugins: list[Plugin], hook: str, ctx: Context) -> dict[str, Any]:
    """Call a reporting hook (status/check) on each plugin, name -> its report.

    A plugin that raises gets an ``{"error": ...}`` entry instead of being
    dropped, so a broken plugin is visible in the report rather than silent.
    """
    out: dict[str, Any] = {}
    for p in plugins:
        if not p.has(hook):
            continue
        try:
            out[p.name] = p.call(hook, ctx)
        except Exception as e:
            warn(f"plugin {p.name!r} failed during {hook}: {e}")
            out[p.name] = {"ok": False, "error": str(e)}
    return out
