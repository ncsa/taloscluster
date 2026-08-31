"""Create Neutron resources with ownership tags across API versions."""

from __future__ import annotations

from typing import Any

from openstack import exceptions

from ..errors import ReconcileError


def create_tagged(proxy: Any, kind: str, tags: list[str], **attrs: Any) -> Any:
    """Create with tags, then verify/fix ownership across Neutron versions."""
    create = getattr(proxy, f"create_{kind}")
    try:
        resource = create(**attrs, tags=tags)
    except exceptions.BadRequestException as e:
        if "attribute 'tags' not allowed in post" not in str(e).lower():
            raise
        resource = create(**attrs)

    try:
        if not set(tags).issubset(proxy.get_tags(resource)):
            resource = proxy.set_tags(resource, tags)
        if not set(tags).issubset(proxy.get_tags(resource)):
            raise ReconcileError(f"Neutron did not persist ownership tags on {kind}")
    except Exception:
        delete = getattr(proxy, f"delete_{kind}")
        delete(resource.id)
        raise
    return resource
