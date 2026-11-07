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

"""Batched HTTP score-service reranking with global candidate ordering."""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANKER_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Rerank Memory hits through one offline HTTP score service."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANKER_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty string")  # noqa: TRY003
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate in bounded batches and select globally."""

        if not isinstance(query, str) or not query:
            raise ValueError("rerank query must be a non-empty string")  # noqa: TRY003
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("rerank limit must be an integer")  # noqa: TRY003
        if not candidates:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003

        scores: dict[int, float] = {}
        requests = 0
        total = len(candidates)
        for start in range(0, total, self._batch_size):
            batch = tuple(
                (rank, candidates[rank - 1])
                for rank in range(start + 1, min(start + self._batch_size, total) + 1)
            )
            await self._submit_batch(query, batch, scores)
            requests += 1

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )

    async def _submit_batch(
        self,
        query: str,
        batch: tuple[tuple[int, MemoryHit], ...],
        scores: dict[int, float],
    ) -> None:
        payload = {
            "query": query,
            "candidates": [
                {"rank": rank, "text": hit.text} for rank, hit in batch
            ],
        }
        response = await self._client.post(self._endpoint, json=payload)
        response.raise_for_status()
        body = _parse_response(response)
        expected_ranks = {rank for rank, _ in batch}
        observed_ranks: set[int] = set()
        for item in body:
            rank = item["rank"]
            if rank in observed_ranks:
                raise ValueError(f"rerank service returned duplicate rank {rank!r}")  # noqa: TRY003
            observed_ranks.add(rank)
            if rank not in expected_ranks:
                raise ValueError(  # noqa: TRY003
                    f"rerank service returned rank {rank!r} outside the requested batch"
                )
            scores[rank] = item["score"]
        if observed_ranks != expected_ranks:
            missing = sorted(expected_ranks - observed_ranks)
            raise ValueError(f"rerank service response is missing ranks {missing}")  # noqa: TRY003


def _parse_response(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank service response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("rerank service response must contain exactly a scores field")  # noqa: TRY003
    items = payload["scores"]
    if not isinstance(items, list):
        raise ValueError("rerank service scores must be a list")  # noqa: TRY003
    parsed: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError(  # noqa: TRY003
                "every rerank score must contain exactly rank and score fields"
            )
        rank = item["rank"]
        score = item["score"]
        if type(rank) is not int or rank < 1:
            raise ValueError("rerank score rank must be a positive integer")  # noqa: TRY003
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")  # noqa: TRY003
        parsed.append({"rank": rank, "score": float(score)})
    return parsed
