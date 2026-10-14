"""Repository abstraction.

Services depend on the ``Repository`` protocol, not on SQLAlchemy. Phase 1 ships an in-memory
implementation (used by tests and mock pipelines); PostgreSQL-backed repositories are added by
the phase that first persists each entity.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.core.errors import NotFoundError
from app.domain.models import Entity


class Repository[T: Entity](Protocol):
    def add(self, entity: T) -> T: ...

    def get(self, entity_id: UUID) -> T: ...

    def list(self) -> list[T]: ...


class InMemoryRepository[T: Entity]:
    def __init__(self) -> None:
        self._items: dict[UUID, T] = {}

    def add(self, entity: T) -> T:
        self._items[entity.id] = entity
        return entity

    def get(self, entity_id: UUID) -> T:
        try:
            return self._items[entity_id]
        except KeyError:
            raise NotFoundError("entity not found", details={"id": str(entity_id)}) from None

    def list(self) -> list[T]:
        return list(self._items.values())
