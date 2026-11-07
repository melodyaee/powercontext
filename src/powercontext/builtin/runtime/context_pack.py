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

"""Deterministic context packs with prefix-reuse deltas for incremental transport."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PREFIX = "sha256:"
_DEFAULT_CHUNK_BYTES = 512


class ContextPackError(ValueError):
    """Raised when a context pack or delta violates the wire protocol."""


def _canonical_unsigned(items: tuple[bytes, ...]) -> dict[str, object]:
    return {
        "schema": CONTEXT_PACK_SCHEMA,
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }


def _digest_for(items: tuple[bytes, ...]) -> str:
    canonical = json.dumps(_canonical_unsigned(items), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(canonical).hexdigest()


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError(f"{field} must be a sha256 digest string")  # noqa: TRY003
    hex_part = value[len(_DIGEST_PREFIX) :]
    if len(hex_part) != 64 or any(character not in "0123456789abcdef" for character in hex_part):
        raise ContextPackError(f"{field} must be a sha256 digest string")  # noqa: TRY003
    return value


def _decode_items(value: object, field: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{field} must be a list of base64 strings")  # noqa: TRY003
    items: list[bytes] = []
    for element in value:
        if not isinstance(element, str):
            raise ContextPackError(f"{field} must contain base64 strings only")  # noqa: TRY003
        try:
            items.append(base64.b64decode(element, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError(f"{field} contains invalid base64 data") from error  # noqa: TRY003
    return tuple(items)


class ContextPack:
    """One complete context payload split into verified deterministic chunks."""

    __slots__ = ("items", "digest", "byte_count")

    def __init__(self, items: tuple[bytes, ...]) -> None:
        self.items: tuple[bytes, ...] = tuple(items)
        self.digest: str = _digest_for(self.items)
        self.byte_count: int = len(self.to_bytes())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextPack):
            return NotImplemented
        return self.items == other.items

    def __hash__(self) -> int:
        return hash(self.items)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ContextPack(items={len(self.items)}, byte_count={self.byte_count})"

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build one complete pack from a tuple of opaque byte chunks."""

        return cls(items)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk one UTF-8 context into bounded, valid chunks deterministically."""

        if not isinstance(content, str):
            raise ContextPackError("content must be a string")  # noqa: TRY003
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        encoded = content.encode("utf-8")
        chunks: list[bytes] = []
        start = 0
        length = 0
        for character in content:
            width = len(character.encode("utf-8"))
            if length + width > chunk_bytes:
                if length == 0:
                    raise ContextPackError(  # noqa: TRY003
                        "chunk_bytes is too small to hold one encoded character"
                    )
                chunks.append(encoded[start : start + length])
                start += length
                length = width
            else:
                length += width
        if length:
            chunks.append(encoded[start : start + length])
        return cls(tuple(chunks))

    def to_bytes(self) -> bytes:
        """Serialize this pack with its self-consistent byte count."""

        unsigned = _canonical_unsigned(self.items)
        byte_count = 0
        while True:
            payload = {**unsigned, "digest": self.digest, "byte_count": byte_count}
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) == byte_count:
                return encoded
            byte_count = len(encoded)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Validate and decode one serialized complete pack."""

        payload = _parse_json_object(data)
        if set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("pack wire fields do not match the schema")  # noqa: TRY003
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack schema is not supported")  # noqa: TRY003
        items = _decode_items(payload["items"], "items")
        digest = _require_digest(payload["digest"], "digest")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count != len(data):
            raise ContextPackError("pack byte_count does not match the wire bytes")  # noqa: TRY003
        if digest != _digest_for(items):
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        return cls(items)


class ContextPackDelta:
    """One incremental update that reuses a base pack's leading chunks."""

    __slots__ = ("base_digest", "target_digest", "prefix_items", "appended")

    def __init__(
        self,
        base_digest: str,
        target_digest: str,
        prefix_items: int,
        appended: tuple[bytes, ...],
    ) -> None:
        self.base_digest: str = _require_digest(base_digest, "base_digest")
        self.target_digest: str = _require_digest(target_digest, "target_digest")
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
        self.prefix_items: int = prefix_items
        self.appended: tuple[bytes, ...] = tuple(appended)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextPackDelta):
            return NotImplemented
        return (
            self.base_digest == other.base_digest
            and self.target_digest == other.target_digest
            and self.prefix_items == other.prefix_items
            and self.appended == other.appended
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ContextPackDelta(prefix_items={self.prefix_items}, "
            f"appended={len(self.appended)}, byte_count={self.byte_count})"
        )

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Reuse the leading identical chunks of both packs."""

        prefix = 0
        maximum = min(len(base.items), len(target.items))
        while prefix < maximum and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        """Serialize this delta with its exact protocol overhead."""

        payload = {
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [base64.b64encode(item).decode("ascii") for item in self.appended],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Validate and decode one serialized delta."""

        payload = _parse_json_object(data)
        if set(payload) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}:
            raise ContextPackError("delta wire fields do not match the schema")  # noqa: TRY003
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta schema is not supported")  # noqa: TRY003
        base_digest = _require_digest(payload["base_digest"], "base_digest")
        target_digest = _require_digest(payload["target_digest"], "target_digest")
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
        appended = _decode_items(payload["appended"], "appended")
        return cls(base_digest, target_digest, prefix_items, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from one base pack and one validated delta."""

    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the base pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack chunk count")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack digest does not match the delta target")  # noqa: TRY003
    return target


def _parse_json_object(data: bytes) -> dict[str, object]:
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("wire payload must be UTF-8") from error  # noqa: TRY003
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContextPackError("wire payload must be a JSON object") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError("wire payload must be a JSON object")  # noqa: TRY003
    return payload


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
