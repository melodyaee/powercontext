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

"""Cross-call Embedding request coalescing with a bounded success-only LRU cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage


class _Slot:
    """One in-flight text identity shared by every caller waiting for it."""

    __slots__ = ("text", "future")

    def __init__(self, text: str, loop: asyncio.AbstractEventLoop) -> None:
        self.text = text
        self.future: asyncio.Future[tuple[EmbeddingVector, int, InferenceUsage]] = loop.create_future()


class CoalescingEmbeddingModel:
    """Deduplicate concurrent Embedding work and reuse successfully produced vectors.

    Provider work always runs in an owned background task. Waiters observe it
    through ``asyncio.shield``, so cancelling one caller never cancels the shared
    request or loses another caller's result.
    """

    def __init__(
        self,
        model: Any,
        /,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._pending: dict[str, _Slot] = {}
        self._inflight: dict[str, _Slot] = {}
        self._flush_handle: asyncio.TimerHandle | asyncio.Handle | None = None
        self._batch_counter = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        ordered = tuple(texts)
        if not ordered:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = asyncio.get_running_loop()
        slots: dict[str, _Slot | None] = {}
        vectors: dict[str, EmbeddingVector] = {}
        # Register every identity before the first await so concurrently
        # released callers join the same coalescing window.
        for text in ordered:
            if text in slots:
                continue
            cached = self._cache.get(text) if self._cache_size else None
            if cached is not None:
                self._cache.move_to_end(text)
                vectors[text] = cached
                slots[text] = None
                continue
            slot = self._pending.get(text) or self._inflight.get(text)
            if slot is None:
                slot = _Slot(text, loop)
                self._pending[text] = slot
                self._schedule_flush(loop)
            slots[text] = slot

        waiting = tuple(slot for slot in slots.values() if slot is not None)
        if not waiting:
            return EmbeddingResult(
                vectors=tuple(vectors[text] for text in ordered),
                usage=InferenceUsage(requests=0),
            )

        resolved = await asyncio.gather(*(asyncio.shield(slot.future) for slot in waiting))
        seen_batches: set[int] = set()
        requests = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        for slot, (vector, batch_id, usage) in zip(waiting, resolved, strict=True):
            vectors[slot.text] = vector
            if batch_id in seen_batches:
                continue
            seen_batches.add(batch_id)
            requests += usage.requests
            if usage.input_tokens is not None:
                input_tokens = (input_tokens or 0) + usage.input_tokens
            if usage.output_tokens is not None:
                output_tokens = (output_tokens or 0) + usage.output_tokens
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in ordered),
            usage=InferenceUsage(
                requests=requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    def _schedule_flush(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._flush_handle is not None:
            return
        if self._coalesce_seconds <= 0:
            self._flush_handle = loop.call_soon(self._flush)
        else:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._pending:
            return
        pending = self._pending
        self._pending = {}
        items = tuple(pending.items())
        for text, slot in items:
            self._inflight[text] = slot
        for start in range(0, len(items), self._max_batch_size):
            group = items[start : start + self._max_batch_size]
            self._batch_counter += 1
            asyncio.create_task(  # noqa: RUF006 - owned by the completion callback below
                self._run_batch(
                    tuple(text for text, _ in group),
                    tuple(slot for _, slot in group),
                    self._batch_counter,
                ),
                name=f"powercontext-embedding-batch-{self._batch_counter}",
            )

    async def _run_batch(self, texts: tuple[str, ...], slots: tuple[_Slot, ...], batch_id: int) -> None:
        try:
            try:
                result = await self._model.embed(texts)
                produced = result.vectors
                if len(produced) != len(texts):
                    raise ValueError(  # noqa: TRY003, TRY301
                        f"embedding provider returned {len(produced)} vectors for {len(texts)} texts"
                    )
                usage = result.usage or InferenceUsage(requests=0)
                normalized = tuple(tuple(vector) for vector in produced)
                if self._cache_size:
                    for text, vector in zip(texts, normalized, strict=True):
                        self._cache[text] = vector
                        self._cache.move_to_end(text)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                for slot, vector in zip(slots, normalized, strict=True):
                    if not slot.future.done():
                        slot.future.set_result((vector, batch_id, usage))
            except BaseException as error:
                # A failed batch is never cached and never cancels an unrelated
                # batch. Every dependent waiter observes the same provider error.
                for slot in slots:
                    if not slot.future.done():
                        slot.future.set_exception(error)
        finally:
            for text in texts:
                self._inflight.pop(text, None)


__all__ = ["CoalescingEmbeddingModel"]
