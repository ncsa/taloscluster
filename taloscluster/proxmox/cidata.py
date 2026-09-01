"""Build short-lived NoCloud cidata ISO images containing Talos machine config."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..errors import ReconcileError


def build(source: Path, destination: Path, hostname: str, machine_config: str) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "user-data").write_text(machine_config)
    (source / "meta-data").write_text(f"instance-id: {hostname}\nlocal-hostname: {hostname}\n")

    if shutil.which("xorriso"):
        cmd = [
            "xorriso", "-as", "mkisofs", "-V", "CIDATA", "-J", "-r",
            "-o", str(destination), str(source),
        ]
    elif shutil.which("genisoimage"):
        cmd = [
            "genisoimage", "-V", "CIDATA", "-J", "-r",
            "-o", str(destination), str(source),
        ]
    elif shutil.which("hdiutil"):
        cmd = [
            "hdiutil", "makehybrid", "-iso", "-joliet",
            "-default-volume-name", "CIDATA", "-o", str(destination), str(source),
        ]
    else:
        raise ReconcileError(
            "creating Proxmox cidata requires xorriso, genisoimage, or hdiutil"
        )
    subprocess.run(cmd, check=True, capture_output=True, text=True)
