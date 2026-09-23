"""Keep the usage page's resize claims in lockstep with the providers.

The "Resize or change access" section of the usage page tells the reader what
converge applies in place and what it refuses. Its OpenStack paragraph once
said a server's ``flavor``, ``disk`` and ``availability_zone`` "affect new
servers only", while the validate phase refuses the change on an existing
server -- a reader who trusted the old wording would edit ``cluster.yaml``,
converge would stop, and the refusal would read like a tool bug. These tests
pin the paragraph to the refusal the code actually raises.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ROOT / "docs" / "usage.md"
OPENSTACK_COMPUTE = ROOT / "taloscluster" / "openstack" / "compute.py"


def _resize_section() -> str:
    text = USAGE.read_text()
    return text.split("## Resize or change access", 1)[1].split("\n## ", 1)[0]


def test_openstack_paragraph_states_the_refusal():
    # the stale claim: the keys "affect new servers only". Converge refuses the
    # change on an existing server instead, and the paragraph must say so and
    # point at the pool reference that documents the refusal.
    assert "affect new servers only" not in USAGE.read_text()
    paragraph = _resize_section()
    for key in ("`flavor`", "`disk`", "`availability_zone`"):
        assert key in paragraph
    assert "refused" in paragraph
    assert "configuration/pools.md" in paragraph


def test_paragraph_agrees_with_the_validate_refusal():
    # the paragraph must not promise more than validate delivers: the refusal
    # names create-only servers and the scale-down recreation path.
    code = OPENSTACK_COMPUTE.read_text()
    assert "refusing unsupported change to existing server" in code
    assert "Servers are create-only on OpenStack" in code
    assert "scaling its pool down past it and back up" in code
