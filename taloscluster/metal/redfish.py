"""Minimal Redfish client for the BMC actions the metal join flow needs.

Talks basic auth to a controller on the scheme its `bmc.scheme` names -- https
by default, so the BMC password never rides an unencrypted link unless the
configuration opts into plain http for a BMC that serves no TLS. The BMC's
certificate is not verified unless `bmc.tls_verify` says otherwise: `true`
checks it against the system trust store and a path pins the CA bundle to
trust, which a management network where the BMC could be impersonated needs.
Only what the commands use is implemented: the power state and reset actions,
the one-time boot override, virtual media insert/eject, and the NIC/disk
summaries `inspect` prints.

Deliberately absent: BIOS attribute changes and persistent boot-order
manipulation. The flow mounts media, one-time boots it and manages power;
after that boot the machine falls back to its own boot order, which for an
installed machine is its disk.
"""

from __future__ import annotations

import requests
import urllib3

from ..config import MetalBmc
from ..errors import ReconcileError

TIMEOUT = 30.0


class RedfishError(ReconcileError):
    """The BMC could not be reached or refused an action."""


class Redfish:
    """A Redfish controller for one machine's BMC."""

    def __init__(self, bmc: MetalBmc, timeout: float = TIMEOUT):
        self.bmc = bmc
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (bmc.username, bmc.password)
        self.session.verify = bmc.tls_verify
        if not bmc.tls_verify:
            # the default trusts the BMC's self-signed certificate; the pinning
            # `tls_verify` asks for must not be muted with it
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._base: str = ""
        self._system_path: str = ""

    # -- transport ----------------------------------------------------------

    @property
    def base(self) -> str:
        """The controller's root URL, on the configured scheme."""
        if not self._base:
            self._base = self._discover_base()
        return self._base

    def _discover_base(self) -> str:
        base = f"{self.bmc.scheme}://{self.bmc.ip}"
        try:
            # the service discovery endpoint proves the scheme and host
            # answer; it needs no authentication
            requests.get(
                f"{base}/redfish/", timeout=self.timeout, verify=self.bmc.tls_verify
            ).close()
        except requests.RequestException as e:
            raise RedfishError(
                f"could not reach a Redfish controller at {self.bmc.ip}: {e}"
            ) from e
        return base

    def _request(self, method: str, path: str, body: dict | None = None) -> requests.Response:
        url = f"{self.base}{path}"
        try:
            return self.session.request(method, url, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise RedfishError(
                f"could not reach the Redfish controller at {self.bmc.ip}: {e}"
            ) from e

    def _checked(self, resp: requests.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        detail = (resp.text or "").strip().replace("\n", " ")[:200]
        raise RedfishError(f"{what} failed (HTTP {resp.status_code}): {detail}")

    def _get(self, path: str) -> dict:
        if not path:
            return {}
        resp = self._request("GET", path)
        self._checked(resp, f"reading {path}")
        try:
            return resp.json()
        except ValueError as e:
            raise RedfishError(f"{self.bmc.ip} returned non-JSON for {path}") from e

    def _post(self, path: str, body: dict) -> requests.Response:
        return self._request("POST", path, body)

    def _patch(self, path: str, body: dict) -> requests.Response:
        return self._request("PATCH", path, body)

    def _members(self, path: str) -> list[str]:
        """The @odata.id of every member of a collection."""
        if not path:
            return []
        doc = self._get(path)
        return [
            str(member["@odata.id"])
            for member in (doc.get("Members") or [])
            if isinstance(member, dict) and member.get("@odata.id")
        ]

    # -- system -------------------------------------------------------------

    def system(self) -> tuple[str, dict]:
        """(resource path, document) of the machine's computer system.

        Single-node servers expose one; the first member is used.
        """
        if not self._system_path:
            members = self._members("/redfish/v1/Systems")
            if not members:
                raise RedfishError(f"{self.bmc.ip} reports no computer systems")
            self._system_path = members[0]
        return self._system_path, self._get(self._system_path)

    def power_state(self) -> str:
        _, doc = self.system()
        return str(doc.get("PowerState") or "")

    def power_on(self) -> None:
        """Power the machine on; force a restart when it already runs, since a
        one-time boot override only takes effect on the next boot."""
        if self.power_state() == "On":
            self.reset("ForceRestart")
        else:
            self.reset("On")

    def reset(self, reset_type: str) -> None:
        _, doc = self.system()
        target = ((doc.get("Actions") or {}).get("#ComputerSystem.Reset") or {}).get("target")
        if not target:
            raise RedfishError(f"{self.bmc.ip} exposes no power-control action")
        self._checked(
            self._post(target, {"ResetType": reset_type}),
            f"{reset_type} on {self.bmc.ip}",
        )

    def boot_once_cd(self) -> None:
        """Set a one-time boot from the virtual media, leaving the persistent
        boot order and any BIOS boot-mode setting alone."""
        path, doc = self.system()
        boot = doc.get("Boot") or {}
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues") or []
        if allowed and "Cd" not in [str(v) for v in allowed]:
            raise RedfishError(
                f"{self.bmc.ip} does not offer a one-time CD boot "
                f"(allows: {', '.join(str(v) for v in allowed)})"
            )
        self._checked(
            self._patch(
                path,
                {
                    "Boot": {
                        "BootSourceOverrideEnabled": "Once",
                        "BootSourceOverrideTarget": "Cd",
                    }
                },
            ),
            f"setting the one-time boot device on {self.bmc.ip}",
        )

    # -- virtual media --------------------------------------------------------

    def virtual_media(self) -> list[tuple[str, dict]]:
        """(resource path, document) of every virtual-media device.

        The collection hangs off the system on some controllers and off each
        manager on others; look in both places.
        """
        roots: list[str] = []
        _, sysdoc = self.system()
        link = sysdoc.get("VirtualMedia")
        if isinstance(link, dict) and link.get("@odata.id"):
            roots.append(str(link["@odata.id"]))
        else:
            for manager in self._members("/redfish/v1/Managers"):
                mdoc = self._get(manager)
                link = mdoc.get("VirtualMedia")
                if isinstance(link, dict) and link.get("@odata.id"):
                    roots.append(str(link["@odata.id"]))
        found: list[tuple[str, dict]] = []
        for root in roots:
            for path in self._members(root):
                found.append((path, self._get(path)))
        return found

    def insert_media(self, image_url: str) -> None:
        """Mount `image_url` as virtual media.

        Controllers expose the InsertMedia action (Dell, HPE, Supermicro) or
        expect an Inserted patch; use whichever the chosen device offers. The
        action takes the image alone -- `Inserted` and `WriteProtected` are
        resource properties, so only the patch fallback sends them. The
        CD/DVD-shaped device is preferred when a controller exposes more
        than one.
        """
        devices = self.virtual_media()
        if not devices:
            raise RedfishError(f"{self.bmc.ip} exposes no virtual media device")
        cd = [
            (path, doc)
            for path, doc in devices
            if "cd" in f"{doc.get('Id', '')} {doc.get('Name', '')}".lower()
            or "dvd" in f"{doc.get('Id', '')} {doc.get('Name', '')}".lower()
        ]
        path, doc = (cd or devices)[0]
        target = ((doc.get("Actions") or {}).get("#VirtualMedia.InsertMedia") or {}).get("target")
        if target:
            resp = self._post(str(target), {"Image": image_url})
        else:
            resp = self._patch(
                path, {"Image": image_url, "Inserted": True, "WriteProtected": True}
            )
        self._checked(resp, f"mounting {image_url} on {self.bmc.ip}")

    def eject_media(self) -> bool:
        """Eject whatever virtual media is mounted; False when none is."""
        for path, doc in self.virtual_media():
            if not doc.get("Inserted"):
                continue
            actions = doc.get("Actions") or {}
            target = (actions.get("#VirtualMedia.EjectMedia") or {}).get("target")
            if target:
                resp = self._post(str(target), {})
            else:
                resp = self._patch(path, {"Inserted": False, "Image": None})
            self._checked(resp, f"ejecting the virtual media of {self.bmc.ip}")
            return True
        return False

    # -- inspect --------------------------------------------------------------

    def summary(self) -> dict:
        """The power/boot/NIC/disk summary `inspect` prints."""
        _, doc = self.system()
        boot = doc.get("Boot") or {}
        return {
            "power": str(doc.get("PowerState") or "unknown"),
            "boot": {
                "override": str(boot.get("BootSourceOverrideEnabled") or ""),
                "target": str(boot.get("BootSourceOverrideTarget") or ""),
            },
            "nics": self._nics(doc),
            "disks": self._disks(doc),
        }

    def _nics(self, sysdoc: dict) -> list[dict]:
        collection = sysdoc.get("EthernetInterfaces")
        link = collection.get("@odata.id") if isinstance(collection, dict) else None
        nics = []
        for path in self._members(str(link or "")):
            nic = self._get(path)
            status = nic.get("Status") or {}
            nics.append(
                {
                    "interface": str(nic.get("Id") or path.rsplit("/", 1)[-1]),
                    "mac": str(nic.get("MACAddress") or ""),
                    "link": str(nic.get("LinkStatus") or status.get("State") or ""),
                }
            )
        return nics

    def _disks(self, sysdoc: dict) -> list[dict]:
        collection = sysdoc.get("Storage")
        link = collection.get("@odata.id") if isinstance(collection, dict) else None
        disks = []
        for storage in self._members(str(link or "")):
            sdoc = self._get(storage)
            for ref in sdoc.get("Drives") or []:
                drive_path = ref.get("@odata.id") if isinstance(ref, dict) else ref
                drive = self._get(str(drive_path or ""))
                if not drive:
                    continue
                disks.append(
                    {
                        "drive": str(drive.get("Id") or str(drive_path).rsplit("/", 1)[-1]),
                        "model": str(drive.get("Model") or ""),
                        "capacity": _human_bytes(drive.get("CapacityBytes")),
                    }
                )
        return disks


def _human_bytes(size: object) -> str:
    """A byte count as the largest unit that keeps one decimal (`953.9 GiB`)."""
    try:
        value = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""
