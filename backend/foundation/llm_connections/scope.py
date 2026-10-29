"""Who a model call is made for.

The owner is bound at the two trusted boundaries where it is known: an
authenticated desktop/API request, and a graph activity resumed from a run
record that persisted its actor. Model clients read it at call time, so the
code in between needs no knowledge of users or keys.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from backend.foundation.llm_connections.models import ConnectionOwner

_CURRENT_OWNER: ContextVar[ConnectionOwner | None] = ContextVar("llm_connection_owner", default=None)


@contextmanager
def llm_call_scope(owner: ConnectionOwner) -> Iterator[ConnectionOwner]:
    if not isinstance(owner, ConnectionOwner):
        raise TypeError("owner must be a ConnectionOwner")
    token = _CURRENT_OWNER.set(owner)
    try:
        yield owner
    finally:
        _CURRENT_OWNER.reset(token)


def current_llm_owner() -> ConnectionOwner | None:
    return _CURRENT_OWNER.get()


def owner_from_actor_metadata(metadata: Mapping[str, Any] | None) -> ConnectionOwner | None:
    """Rebuild the owner from persisted actor metadata (``user_id`` / ``tenant_id``)."""
    if not isinstance(metadata, Mapping):
        return None
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    tenant_id = metadata.get("tenant_id")
    tenant_id = tenant_id.strip() if isinstance(tenant_id, str) and tenant_id.strip() else None
    try:
        return ConnectionOwner(user_id.strip(), tenant_id=tenant_id)
    except ValueError:
        return None


__all__ = ["current_llm_owner", "llm_call_scope", "owner_from_actor_metadata"]
