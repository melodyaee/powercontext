# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Source-driven eligibility propagation for native Memory and Topic Memory."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.artifacts.models import MemoryCitation
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
)
from powercontext.sources.models import SourceRef

_NODES = SOURCE_LIFECYCLE_NODES_TABLE
_EVENTS = SOURCE_LIFECYCLE_EVENTS_TABLE
_REFERENCE_TYPES = {"source": SourceRef, "memory": MemoryCitation, "topic": ArtifactRef}
_VALID_POLICIES = frozenset({"all", "any"})


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration or event violates its contract."""


class SourceLifecycleEvent(BaseModel):
    """One versioned availability transition for exactly one registered Source."""

    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-blank string")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")  # noqa: TRY003
        return value


@dataclass(frozen=True)
class _Node:
    node_id: str
    kind: str
    parents: tuple[str, ...]
    policy: str
    reference: Any
    revision: int
    available: bool
    last_sequence: int


def _encode_node(scope_id: str, node: _Node) -> dict[str, object]:
    return {
        "scope_id": scope_id,
        "node_id": node.node_id,
        "kind": node.kind,
        "parents": json.dumps(list(node.parents), ensure_ascii=False),
        "policy": node.policy,
        "reference": json.dumps(node.reference.model_dump(mode="json"), sort_keys=True, ensure_ascii=False),
        "revision": node.revision,
        "available": node.available,
        "last_sequence": node.last_sequence,
    }


def _decode_node(row: Any) -> _Node:
    kind = str(row["kind"])
    reference_type = _REFERENCE_TYPES[kind]
    return _Node(
        node_id=str(row["node_id"]),
        kind=kind,
        parents=tuple(json.loads(row["parents"])),
        policy=str(row["policy"]),
        reference=reference_type.model_validate(json.loads(row["reference"])),
        revision=int(row["revision"]),
        available=bool(row["available"]),
        last_sequence=int(row["last_sequence"]),
    )


async def _load_nodes(connection: AsyncConnection, scope_id: str) -> dict[str, _Node]:
    statement = (
        select(_NODES)
        .where(_NODES.c.scope_id == scope_id)
        .order_by(_NODES.c.node_id)
    )
    rows = (await connection.execute(statement)).mappings()
    return {str(row["node_id"]): _decode_node(row) for row in rows}


async def _upsert_node(connection: AsyncConnection, scope_id: str, node: _Node) -> None:
    values = _encode_node(scope_id, node)
    where = (_NODES.c.scope_id == scope_id, _NODES.c.node_id == node.node_id)
    exists = await connection.scalar(select(func.count()).select_from(_NODES).where(*where))
    if exists:
        await connection.execute(update(_NODES).where(*where).values(**{k: v for k, v in values.items() if k not in {"scope_id", "node_id"}}))
    else:
        await connection.execute(insert(_NODES).values(**values))


def _recompute(nodes: dict[str, _Node]) -> set[str]:
    """Propagate eligibility to a fixed point, independent of row/insertion order.

    A single pass is only correct when every parent happens to be visited before its
    children. Persisted rows come back ordered by node id, which is frequently *not* a
    topological order for a 16-20 node graph, so a child whose parent is itself derived
    would be judged against a stale support set. Availability is monotone here (nodes are
    only added), so repeated passes always converge in at most ``len(nodes)`` rounds.
    """

    active = {node_id for node_id, node in nodes.items() if node.kind == "source" and node.available}
    changed = True
    while changed:
        changed = False
        for node_id, node in nodes.items():
            if node.kind == "source" or node_id in active:
                continue
            support = tuple(parent in active for parent in node.parents)
            supported = all(support) if node.policy == "all" else any(support)
            if supported:
                active.add(node_id)
                changed = True
    return active


async def load_disabled_topics(database: Any) -> dict[str, set[str]]:
    """Restore native Topic exclusions after a Runtime restart."""

    disabled: dict[str, set[str]] = {}
    async with database.transaction() as connection:
        rows = (
            await connection.execute(
                select(_NODES).where(_NODES.c.kind == "topic", _NODES.c.available.is_(False))
            )
        ).mappings()
        for row in rows:
            reference = ArtifactRef.model_validate(json.loads(row["reference"]))
            disabled.setdefault(str(row["scope_id"]), set()).add(reference.artifact_id)
    return disabled


class SourceLifecycleApplication:
    """Runtime facade exposing one independent eligibility graph per Scope."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)


class ScopedSourceLifecycle:
    """Register evidence bindings and propagate Source availability events."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    @property
    def _database(self) -> Any:
        database = getattr(self._runtime, "_lifecycle_database", None)
        if database is None:
            database = self._runtime._provider.database
        return database

    @staticmethod
    def _identity(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SourceLifecycleError("node_id must be a non-blank string")  # noqa: TRY003
        return value

    async def _readable(self, kind: str, reference: Any) -> None:
        scope = self.scope_id
        try:
            if kind == "source":
                await self._runtime.records.for_scope(scope).get_source(
                    reference.source_type, reference.source_id
                )
            elif kind == "memory":
                from powercontext.builtin.runtime.models import GetMemoryEntryRequest

                await self._runtime.memory.for_scope(scope).get(GetMemoryEntryRequest(citation=reference))
            else:
                await self._runtime.records.for_scope(scope).get_artifact_revision(
                    "topic-memory", reference.artifact_id, reference.revision
                )
        except Exception as error:
            raise SourceLifecycleError(f"registered reference is not readable: {error}") from error  # noqa: TRY003

    async def _register(
        self,
        node_id: str,
        reference: Any,
        *,
        kind: str,
        parents: tuple[str, ...],
        policy: str,
        revision: int,
    ) -> None:
        node_id = self._identity(node_id)
        if policy not in _VALID_POLICIES:
            raise SourceLifecycleError("policy must be 'all' or 'any'")  # noqa: TRY003
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")  # noqa: TRY003
        await self._readable(kind, reference)
        async with self._database.transaction() as connection:
            nodes = await _load_nodes(connection, self.scope_id)
            if kind != "source":
                if not parents:
                    raise SourceLifecycleError("a derived node needs at least one parent")  # noqa: TRY003
                if len(set(parents)) != len(parents):
                    raise SourceLifecycleError("parent identifiers must be unique")  # noqa: TRY003
                for parent in parents:
                    if parent not in nodes:
                        raise SourceLifecycleError(f"unknown or cyclic parent: {parent}")  # noqa: TRY003
            candidate = _Node(
                node_id=node_id,
                kind=kind,
                parents=parents,
                policy=policy,
                reference=reference,
                revision=revision,
                available=True,
                last_sequence=0,
            )
            existing = nodes.get(node_id)
            if existing is not None:
                same = (
                    existing.kind == kind
                    and existing.parents == parents
                    and existing.policy == policy
                    and existing.revision == revision
                    and _encode_node(self.scope_id, existing)["reference"]
                    == _encode_node(self.scope_id, candidate)["reference"]
                )
                if same:
                    return
                raise SourceLifecycleError(f"conflicting registration for node: {node_id}")  # noqa: TRY003
            if kind != "source":
                active = _recompute(nodes)
                support = tuple(parent in active for parent in parents)
                candidate = replace(
                    candidate,
                    available=all(support) if policy == "all" else any(support),
                )
            nodes[node_id] = candidate
            await _upsert_node(connection, self.scope_id, candidate)
        active = _recompute(nodes)
        await self._sync_native(nodes, active)

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int = 3) -> None:
        await self._register(
            node_id, source_ref, kind="source", parents=(), policy="all", revision=revision
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        await self._register(
            node_id, citation, kind="memory", parents=tuple(parents), policy=policy, revision=1
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        await self._register(
            node_id, artifact_ref, kind="topic", parents=tuple(parents), policy=policy, revision=1
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        scope = self.scope_id
        async with self._database.transaction() as connection:
            nodes = await _load_nodes(connection, scope)
            node = nodes.get(event.node_id)
            if node is None or node.kind != "source":
                raise SourceLifecycleError(f"unknown Source node: {event.node_id}")  # noqa: TRY003
            if node.revision != event.revision:
                raise SourceLifecycleError("event revision does not match the Source lifecycle version")  # noqa: TRY003
            if event.sequence < node.last_sequence:
                raise SourceLifecycleError("event sequence is older than the latest applied event")  # noqa: TRY003
            where = (
                _EVENTS.c.scope_id == scope,
                _EVENTS.c.node_id == event.node_id,
                _EVENTS.c.sequence == event.sequence,
            )
            stored = (
                await connection.execute(select(_EVENTS).where(*where))
            ).mappings().one_or_none()
            replay = stored is not None
            if replay:
                if (
                    int(stored["revision"]) != event.revision
                    or bool(stored["available"]) != event.available
                    or str(stored["reason"]) != event.reason
                ):
                    raise SourceLifecycleError("conflicting replay of the same event sequence")  # noqa: TRY003
            else:
                await connection.execute(
                    insert(_EVENTS).values(
                        scope_id=scope,
                        node_id=event.node_id,
                        sequence=event.sequence,
                        revision=event.revision,
                        available=event.available,
                        reason=event.reason,
                        applied=False,
                    )
                )
            nodes[event.node_id] = replace(
                node, available=event.available, last_sequence=max(node.last_sequence, event.sequence)
            )
            await _upsert_node(connection, scope, nodes[event.node_id])
            active = _recompute(nodes)
            for node_id, derived in tuple(nodes.items()):
                if derived.kind == "source":
                    continue
                available = node_id in active
                if derived.available != available:
                    nodes[node_id] = replace(derived, available=available)
                    await _upsert_node(connection, scope, nodes[node_id])
            already_applied = bool(stored["applied"]) if replay else False

        if replay and already_applied:
            return sorted(active)
        # Persist the event before touching native state: an interrupted native
        # write keeps the work replayable, and a completed replay is a no-op.
        await self._sync_native(nodes, active)
        async with self._database.transaction() as connection:
            await connection.execute(
                update(_EVENTS)
                .where(
                    _EVENTS.c.scope_id == scope,
                    _EVENTS.c.node_id == event.node_id,
                    _EVENTS.c.sequence == event.sequence,
                )
                .values(applied=True)
            )
        return sorted(active)

    async def resolve(self, node_id: str, /, *, purpose: str) -> Any:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError(f"unknown resolve purpose: {purpose}")  # noqa: TRY003
        async with self._database.transaction() as connection:
            nodes = await _load_nodes(connection, self.scope_id)
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"unknown node: {node_id}")  # noqa: TRY003
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node is unavailable for generation: {node_id}")  # noqa: TRY003
        return node.reference

    async def _sync_native(self, nodes: dict[str, _Node], active: set[str]) -> None:
        disabled = {
            node.reference.artifact_id
            for node_id, node in nodes.items()
            if node.kind == "topic" and node_id not in active
        }
        registry = getattr(self._runtime, "_lifecycle_disabled_topics", None)
        if registry is None:
            registry = {}
            self._runtime._lifecycle_disabled_topics = registry
        registry[self.scope_id] = disabled

        citations = tuple(
            node.reference for node in nodes.values() if node.kind == "memory"
        )
        if not citations:
            return
        inactive = {
            (node.reference.entry_id, node.reference.entry_version_id)
            for node_id, node in nodes.items()
            if node.kind == "memory" and node_id not in active
        }
        citation_keys = {
            (citation.entry_id, citation.entry_version_id) for citation in citations
        }
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            try:
                current = await service.head(context.artifacts.memory_artifact_id)
            except Exception:  # noqa: BLE001 - a Scope without Memory has no state to sync
                return
            for target_state in ("inactive", "active"):
                entries = await service.entries(current)
                selected = tuple(
                    entry
                    for entry in entries
                    if (entry.entry_id, entry.entry_version_id) in citation_keys
                    and ((entry.entry_id, entry.entry_version_id) in inactive)
                    == (target_state == "inactive")
                )
                if not selected:
                    continue
                if target_state == "inactive":
                    await service.forget(current, entries=selected, reason="source-lifecycle")
                else:
                    await service.reactivate(current, entries=selected, reason="source-lifecycle")
                current = await service.head(context.artifacts.memory_artifact_id)


__all__ = ["SourceLifecycleApplication", "SourceLifecycleError", "SourceLifecycleEvent", "load_disabled_topics"]
