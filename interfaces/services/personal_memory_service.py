from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import (
    PersonalMemoryCategory,
    PersonalMemoryOwner,
    PersonalMemoryRecord,
    PersonalMemoryRemoteChannel,
    PersonalMemorySourceRef,
    PersonalMemoryStatus,
)
from interfaces.services.research_desktop_service import ResearchDesktopOwner


class PersonalMemoryApplicationService:
    """Trusted interface adapter for the local personal-memory application."""

    def __init__(
        self,
        application: PersonalMemoryService,
        *,
        database_path: str | Path,
    ) -> None:
        if not isinstance(application, PersonalMemoryService):
            raise TypeError("application must be PersonalMemoryService")
        path = Path(database_path)
        if not path.is_absolute():
            raise ValueError("database_path must be absolute")
        self._application = application
        self._database_path = path

    @property
    def database_path(self) -> Path:
        return self._database_path

    def list(
        self,
        owner: ResearchDesktopOwner,
        *,
        statuses: Iterable[PersonalMemoryStatus | str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        page = self._application.list(
            _personal_memory_owner(owner),
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
        return {
            "records": [_record(record) for record in page.records],
            "total": page.total,
            "next_cursor": (
                str(page.next_offset) if page.next_offset is not None else None
            ),
        }

    def get(self, owner: ResearchDesktopOwner, memory_id: str) -> dict[str, Any]:
        return {
            "record": _record(
                self._application.get(_personal_memory_owner(owner), memory_id)
            )
        }

    def create_candidate(
        self,
        owner: ResearchDesktopOwner,
        *,
        content: str,
        category: PersonalMemoryCategory | str,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
        request_id: str,
    ) -> dict[str, Any]:
        record = self._application.create_candidate(
            _personal_memory_owner(owner),
            content=content,
            category=category,
            source_refs=source_refs,
            client_request_id=request_id,
        )
        return {"record": _record(record)}

    def create_confirmed(
        self,
        owner: ResearchDesktopOwner,
        *,
        content: str,
        category: PersonalMemoryCategory | str,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
        request_id: str,
    ) -> dict[str, Any]:
        record = self._application.create_confirmed(
            _personal_memory_owner(owner),
            content=content,
            category=category,
            source_refs=source_refs,
            client_request_id=request_id,
        )
        return {"record": _record(record)}

    def confirm(
        self,
        owner: ResearchDesktopOwner,
        memory_id: str,
        *,
        revision: int,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self._application.confirm(
            _personal_memory_owner(owner),
            memory_id,
            expected_revision=revision,
            **dict(changes),
        )
        return {"record": _record(record)}

    def reject_candidate(
        self,
        owner: ResearchDesktopOwner,
        memory_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        record = self._application.reject_candidate(
            _personal_memory_owner(owner),
            memory_id,
            expected_revision=revision,
        )
        return {"record": _record(record)}

    def update(
        self,
        owner: ResearchDesktopOwner,
        memory_id: str,
        *,
        revision: int,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self._application.update(
            _personal_memory_owner(owner),
            memory_id,
            expected_revision=revision,
            **dict(changes),
        )
        return {"record": _record(record)}

    def set_status(
        self,
        owner: ResearchDesktopOwner,
        memory_id: str,
        *,
        status: PersonalMemoryStatus | str,
        revision: int,
    ) -> dict[str, Any]:
        normalized = PersonalMemoryStatus(status)
        if normalized is PersonalMemoryStatus.ACTIVE:
            record = self._application.enable(
                _personal_memory_owner(owner),
                memory_id,
                expected_revision=revision,
            )
        elif normalized is PersonalMemoryStatus.DISABLED:
            record = self._application.disable(
                _personal_memory_owner(owner),
                memory_id,
                expected_revision=revision,
            )
        else:
            raise ValueError("status must be active or disabled")
        return {"record": _record(record)}

    def delete(
        self,
        owner: ResearchDesktopOwner,
        memory_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        scoped_owner = _personal_memory_owner(owner)
        current = self._application.get(scoped_owner, memory_id)
        if current.status is PersonalMemoryStatus.CANDIDATE:
            record = self._application.reject_candidate(
                scoped_owner,
                memory_id,
                expected_revision=revision,
            )
        else:
            record = self._application.delete(
                scoped_owner,
                memory_id,
                expected_revision=revision,
            )
        return {"record": _record(record)}

    def recall(
        self,
        owner: ResearchDesktopOwner,
        *,
        query: str,
        limit: int,
        use_memory: bool,
    ) -> dict[str, Any]:
        result = self._application.recall(
            _personal_memory_owner(owner),
            query=query,
            limit=limit,
            use_memory=use_memory,
        )
        return result.to_dict()

    def select_active(
        self,
        owner: ResearchDesktopOwner,
        *,
        memory_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self._application.select_active(
            _personal_memory_owner(owner),
            memory_refs=memory_refs,
        )
        return snapshot.to_dict()

    def rebuild_recall_index(
        self,
        owner: ResearchDesktopOwner,
    ) -> dict[str, int]:
        return {
            "indexed_records": self._application.rebuild_recall_index(
                _personal_memory_owner(owner)
            )
        }

    def export(
        self,
        owner: ResearchDesktopOwner,
        *,
        include_deleted: bool,
    ) -> dict[str, Any]:
        exported = self._application.export(
            _personal_memory_owner(owner),
            include_deleted=include_deleted,
        )
        return {
            "export": {
                "schema_version": exported.schema_version,
                "exported_at": exported.exported_at.isoformat(),
                "records": [_record(record) for record in exported.records],
                "policy": exported.policy.to_dict(include_owner=False),
                "notices": list(exported.notices),
            }
        }

    def remote_processing_permissions(
        self,
        owner: ResearchDesktopOwner,
        *,
        use_memory: bool,
    ) -> dict[str, bool]:
        policy = self._application.get_policy(_personal_memory_owner(owner))
        return {
            channel.value: policy.allows_remote_processing(
                channel,
                use_memory=use_memory,
            )
            for channel in PersonalMemoryRemoteChannel
        }

    def get_policy(self, owner: ResearchDesktopOwner) -> dict[str, Any]:
        policy = self._application.get_policy(_personal_memory_owner(owner))
        return {"policy": policy.to_dict(include_owner=False)}

    def update_policy(
        self,
        owner: ResearchDesktopOwner,
        *,
        allow_remote_processing: bool,
        revision: int,
    ) -> dict[str, Any]:
        policy = self._application.update_policy(
            _personal_memory_owner(owner),
            allow_remote_processing=allow_remote_processing,
            expected_revision=revision,
        )
        return {"policy": policy.to_dict(include_owner=False)}

    def info(self, owner: ResearchDesktopOwner) -> dict[str, Any]:
        _personal_memory_owner(owner)
        return {
            "info": {
                # Server filesystem locations stay private to the operator.
                "storage": "server_sqlite",
                "status": "available",
                "remote_processing_default": False,
                "remote_processing_channel_defaults": {
                    channel.value: False for channel in PersonalMemoryRemoteChannel
                },
                "remote_processing_channels": [
                    channel.value for channel in PersonalMemoryRemoteChannel
                ],
                "remote_processing_auto_enabled": False,
                "remote_processing_policy_endpoint": (
                    "/api/v1/research/desktop/memory/policy"
                ),
            }
        }


def _personal_memory_owner(owner: ResearchDesktopOwner) -> PersonalMemoryOwner:
    if not isinstance(owner, ResearchDesktopOwner):
        raise TypeError("owner must be ResearchDesktopOwner")
    user_id = str(owner.user_id or "").strip()
    namespace = str(owner.memory_namespace or "").strip() or f"research:user:{user_id}"
    return PersonalMemoryOwner(
        user_id=user_id,
        tenant_id=owner.tenant_id,
        namespace=namespace,
    )


def _record(record: PersonalMemoryRecord) -> dict[str, Any]:
    return record.to_dict(include_owner=False)


__all__ = ["PersonalMemoryApplicationService"]
