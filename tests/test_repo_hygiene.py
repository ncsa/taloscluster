"""Keep the real site's data out of the repository.

The checkout doubles as an operator workspace: ``csfarm`` symlinks to a real
cluster directory and ``gen-infra.py`` is local tooling whose docstring once
carried the site's real domain, address and email. Both are git-ignored
("local cluster bootstrap manifests, not part of the tool") and must stay that
way -- a dropped ``.gitignore`` entry would let ``git add .`` commit them, and
the docstring must keep placeholder values (the repository convention) so the
real data survives only in the git-ignored ``infra.yaml``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Local operator files carrying real site data; each must stay covered by
# .gitignore so a plain `git add .` can never pick it up.
LOCAL_SITE_FILES = ("csfarm", "gen-infra.py")

# The real values the gen-infra.py docstring once carried (site domain,
# operator email, site address); only placeholder values may appear there.
REAL_SITE_TOKENS = ("ncsa.cloud", "@illinois.edu", "141.142.36.")


@pytest.mark.parametrize("name", LOCAL_SITE_FILES)
def test_local_site_files_are_git_ignored(name):
    # `git check-ignore` answers for the path name itself, so this also holds
    # on a fresh clone where the git-ignored files do not exist.
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", name],
        cwd=ROOT,
        capture_output=True,
    )
    assert ignored.returncode == 0, f"{name} is no longer covered by .gitignore"


def test_gen_infra_docstring_uses_placeholder_values():
    path = ROOT / "gen-infra.py"
    if not path.is_file():
        pytest.skip("gen-infra.py is gitignored local tooling, not in this checkout")
    text = path.read_text()
    offenders = [token for token in REAL_SITE_TOKENS if token in text]
    assert not offenders, f"real site values back in the gen-infra.py docstring: {offenders}"
