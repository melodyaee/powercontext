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

"""Capture the selected Pi session branch as immutable, citable evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_CONNECTOR_NAME = "pi-session-history"
PI_CONNECTOR_VERSION = "1"
_IDENTITY_PREFIX = "pi"
_IDENTITY_SEPARATOR = ":"
_SESSION_TYPE = "session"
_SUPPORTED_SESSION_VERSION = 3
_SUMMARY_TYPES = ("compaction", "branch_summary")


class PiSessionInput(BaseModel):
    """One offline Pi session archive and the branch leaf to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch with its complete raw record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi event that keeps its full original text and record."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable delimiter-safe identity of one Pi event."""

    identity = _IDENTITY_SEPARATOR.join(
        (_IDENTITY_PREFIX, quote(session_id, safe=""), quote(event_id, safe=""))
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"Pi event identity must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse one Pi session archive and return the selected branch root to leaf."""

    records = _read_records(value.content)
    if not records:
        raise ValueError("Pi session archive must not be empty")  # noqa: TRY003
    header = records[0]
    if not isinstance(header, dict) or header.get("type") != _SESSION_TYPE:
        raise ValueError("the first Pi record must be the session header")  # noqa: TRY003
    if header.get("version") != _SUPPORTED_SESSION_VERSION:
        raise ValueError("only Pi session version 3 is supported")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session id must be a non-empty string")  # noqa: TRY003

    nodes: dict[str, dict[str, JsonValue]] = {}
    order: list[str] = []
    for record in records[1:]:
        if not isinstance(record, dict):
            raise ValueError("every Pi record must be a JSON object")  # noqa: TRY003
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every Pi record must carry a non-empty id")  # noqa: TRY003
        if event_id in nodes or event_id == session_id:
            raise ValueError(f"duplicate Pi record id {event_id!r}")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError(f"Pi record {event_id!r} is missing parentId")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError(f"Pi record {event_id!r} has an invalid parentId")  # noqa: TRY003
            if parent_id != session_id and parent_id not in nodes:
                raise ValueError(  # noqa: TRY003
                    f"Pi record {event_id!r} references a parent that has not appeared yet"
                )
        nodes[event_id] = record
        order.append(event_id)

    leaf_id = value.leaf_id if value.leaf_id is not None else (order[-1] if order else None)
    if leaf_id is None or (leaf_id != session_id and leaf_id not in nodes):
        raise ValueError(f"selected Pi leaf {leaf_id!r} is missing from the session")  # noqa: TRY003

    chain: list[dict[str, JsonValue]] = []
    current: str | None = leaf_id
    while current is not None and current != session_id:
        record = nodes[current]
        chain.append(record)
        parent = record.get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()

    events: list[PiEventInput] = []
    for record in chain:
        event = _visible_event(session_id, record)
        if event is not None:
            events.append(event)
    return tuple(events)


def _read_records(content: bytes) -> list[Any]:
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("Pi session content must be raw bytes")  # noqa: TRY003
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    records: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"Pi session line is not valid JSON: {error}") from error  # noqa: TRY003
    return records


def _visible_event(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type in _SUMMARY_TYPES:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return _event(session_id, record, role=event_type, content=summary)
    if event_type == "custom_message":
        body = _text_blocks(record.get("content"))
        if body is None or not body:
            return None
        return _event(session_id, record, role="custom", content=body)
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "bashExecution":
            command = message.get("command")
            output = message.get("output")
            if not isinstance(command, str):
                return None
            output_text = output if isinstance(output, str) else ""
            return _event(session_id, record, role=role, content=f"$ {command}\n{output_text}")
        if role == "toolResult":
            tool_name = message.get("toolName")
            tool_call_id = message.get("toolCallId")
            if not isinstance(tool_name, str) or not isinstance(tool_call_id, str):
                return None
            body = _text_blocks(message.get("content"))
            return _event(
                session_id,
                record,
                role=role,
                content=f"[tool_result {tool_name} {tool_call_id}] {body or ''}",
            )
        body = _message_body(message)
        if not body:
            return None
        return _event(session_id, record, role=role, content=body)
    return None


def _message_body(message: dict[str, JsonValue]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block_type == "toolCall":
            name = block.get("name")
            call_id = block.get("id")
            if not isinstance(name, str) or not isinstance(call_id, str):
                continue
            arguments = block.get("arguments")
            encoded = json.dumps(
                arguments if arguments is not None else {},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            parts.append(f"[tool_call {name} {call_id}] {encoded}")
    return "\n".join(parts)


def _text_blocks(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _event(
    session_id: str,
    record: dict[str, JsonValue],
    *,
    role: str,
    content: str,
) -> PiEventInput:
    parent_id = record.get("parentId")
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=parent_id if isinstance(parent_id, str) else None,
        event_type=record.get("type"),
        role=role,
        content=f"{role}: {content}",
        record=record,
    )


class _PiEventSourceAdapter:
    """Resolve Pi events into visible Sources and read them back losslessly."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class _PiEventTextEvidenceProjection:
    """Expose one Pi event as canonical text evidence without losing content."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


_PI_EVENT_SOURCE_ADAPTER = _PiEventSourceAdapter()

PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    _PI_EVENT_SOURCE_ADAPTER,
    version="1",
    projections=(_PiEventTextEvidenceProjection(),),
)


def _record_digest(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_checkpoint(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if isinstance(checkpoint, dict):
        events = checkpoint.get("events")
        if isinstance(events, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in events.items()
        ):
            return dict(events)
    raise ValueError("stored Pi session checkpoint is malformed")  # noqa: TRY003


class PiSessionConnector:
    """Incrementally capture the selected branch of a Pi session archive."""

    name = PI_CONNECTOR_NAME
    version = PI_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: Any, /) -> ConnectorRunCompletion:
        content = self.path.read_bytes()
        imported = _decode_checkpoint(session.checkpoint)
        all_records = _index_records(content)
        for event_id, digest in imported.items():
            current = all_records.get(event_id)
            if current is not None and current != digest:
                raise ValueError(  # noqa: TRY003
                    f"imported Pi event {event_id!r} was rewritten in the session archive"
                )

        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self.leaf_id))
        for event in events:
            if event.event_id not in imported:
                await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)

        merged = dict(imported)
        for event in events:
            merged[event.event_id] = _record_digest(event.record)
        return ConnectorRunCompletion(
            status="complete",
            checkpoint={"events": merged},
        )


def _index_records(content: bytes) -> dict[str, str]:
    digests: dict[str, str] = {}
    for record in _read_records(content)[1:]:
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            digests[record["id"]] = _record_digest(record)
    return digests
