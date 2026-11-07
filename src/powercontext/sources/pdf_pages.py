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

"""Physical-page PDF capture and offline evidence-page retrieval."""

from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from collections import Counter
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    SourceDefinition,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.artifacts import MemoryCitation

PDF_PAGE_SOURCE_NAME = "pdf-page"
_PDF_PAGE_SOURCE_PREFIX = "pdf"
_IDENTITY_SEPARATOR = ":"
_SHA256_HEX_LENGTH = 64


class PDFDocumentInput(BaseModel):
    """One complete, unmodified PDF byte stream to be parsed."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page, addressed by the whole-document digest and a 1-based page number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured physical page whose text is stored verbatim for lossless reads."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable, collision-free identity of one physical page."""

    _require_document_id(document_id)
    if not isinstance(document_sha256, str) or len(document_sha256) != _SHA256_HEX_LENGTH:
        raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
    if any(character not in "0123456789abcdef" for character in document_sha256):
        raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    identity = _IDENTITY_SEPARATOR.join(
        (
            _PDF_PAGE_SOURCE_PREFIX,
            quote(document_id, safe=""),
            document_sha256,
            str(page_number),
        )
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"PDF page identity must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of a complete PDF atomically.

    A failure anywhere in the document rejects the whole input; no partial page list is
    ever returned. Blank pages are kept in order with empty text.
    """

    _require_document_id(value.document_id)
    content = value.content
    if not isinstance(content, bytes) or not content:
        raise ValueError("PDF content must be non-empty raw bytes")  # noqa: TRY003
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a user password")  # noqa: TRY003
        pages = tuple(reader.pages)
        texts: list[str] = []
        for page in pages:
            texts.append(page.extract_text() or "")
    except Exception as error:  # noqa: BLE001 - pypdf raises several unrelated error types
        raise ValueError(f"PDF document could not be parsed completely: {error}") from error  # noqa: TRY003
    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


def _require_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-blank string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003


class _PDFPageSourceAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageSourceAdapter(),
    version="1",
    projections=(_PDFPageTextEvidenceProjection(),),
)


# --- Offline evidence-page retrieval -------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")
_CORPUS_LIMIT = 4096
_TARGET_LIMIT = 120
_RRF_CONSTANT = 60.0
_BM25_K1 = 1.5
_BM25_B = 0.75

# Two-term corpus queries keep the built-in lexical admission floor at one matched term,
# so a broad, deterministic slice of all persisted pages is available for offline IDF.
_CORPUS_QUERIES = ("the of", "and to", "2022 2021", "in a for is")

# Question/instruction words carry no evidence in an annual report. Domain terms are
# deliberately kept: corpus IDF, not a hand-written blacklist, decides their weight.
_STOP_TERMS = frozenset(
    """
    a an the and or for of to in on at by from with as is are was were be been being am
    what which who whom whose how why when where do does did can could should would will shall may
    i we you he she they it its our your their this that these those there here please
    has have had any all some no not nor but if then than so such per via about into over under
    answer answers respond response responding question questions ask asking assume assumes assuming
    perspective investment analyst define defined calculate calculated compute give gives given
    state states stated report reported reporting rely relying use used using only show shown shows
    details detail within provide provided providing describe explains explain
    based basis according accordingly respect respectively
    """.split()
)

# Generic financial vocabulary expansion. These are language-level synonyms, not answers,
# document identifiers or evidence-page hints.
_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "ebitda": ("operating", "profit", "income", "depreciation", "amortization", "earnings"),
    "ebit": ("operating", "profit", "income", "earnings"),
    "capex": ("capital", "expenditures", "property", "plant", "equipment", "spending"),
    "margin": ("margin", "percentage", "profit", "revenue", "sales"),
    "margins": ("margin", "percentage", "profit", "revenue"),
    "geographies": ("geographic", "geographical", "regions", "region", "segment", "segments", "international", "america", "europe"),
    "geography": ("geographic", "regions", "segment", "international", "america", "europe"),
    "regions": ("geographic", "segment", "international", "america", "europe"),
    "legal": ("litigation", "proceedings", "contingencies", "lawsuits", "claims"),
    "battles": ("litigation", "proceedings", "lawsuits", "claims", "contingencies"),
    "lawsuit": ("litigation", "proceedings", "claims"),
    "lawsuits": ("litigation", "proceedings", "claims"),
    "litigation": ("legal", "proceedings", "contingencies", "claims"),
    "restructuring": ("restructuring", "impairment", "charges", "severance", "productivity"),
    "income": ("income", "revenue", "earnings", "profit", "sales"),
    "statements": ("statement", "consolidated", "financial", "operations"),
    "statement": ("consolidated", "financial", "operations"),
    "cash": ("cash", "flows", "operating", "activities", "investing", "financing"),
    "flows": ("cash", "operating", "activities", "financing"),
    "flow": ("cash", "operating", "activities", "financing"),
    "unadjusted": ("operating", "profit", "income", "reported"),
    "quantity": ("amount", "total", "value", "costs", "charges"),
    "directly": ("amount", "total", "charges", "costs"),
    "outlined": ("amount", "total", "charges", "costs"),
    "costs": ("costs", "expenses", "charges", "amount"),
    "cost": ("costs", "expenses", "charges", "amount"),
    "revenue": ("revenue", "sales", "net", "income"),
    "revenues": ("revenue", "sales", "net", "income"),
    "assets": ("assets", "balance", "sheet", "liabilities"),
    "liabilities": ("liabilities", "balance", "sheet", "equity"),
    "debt": ("debt", "borrowings", "notes", "long", "term"),
    "eps": ("earnings", "per", "share", "diluted"),
    "operates": ("operations", "operating", "segments", "business", "geographic"),
    "operate": ("operations", "operating", "segments", "business", "geographic"),
    "segments": ("segment", "reportable", "division", "geographic"),
    "employees": ("employees", "workforce", "personnel"),
    "fy": ("fiscal", "year", "annual"),
}

_NUMERIC_INTENT_TERMS = frozenset(
    """
    ebitda ebit capex margin margins revenue revenues income profit loss cost costs expenses
    amount amounts value values total totals quantity percentage percent ratio rate growth
    million billions billion cash assets liabilities debt eps expenditures depreciation
    amortization net gross change increase decreased calculate calculated
    """.split()
)


def _question_terms(question: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", question).casefold()
    return tuple(term for term in _TOKEN_PATTERN.findall(normalized) if term not in _STOP_TERMS and len(term) > 1)


def _query_weights(terms: tuple[str, ...]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for term in terms:
        weight = 0.45 if term.isdigit() else 1.0
        weights[term] = max(weights.get(term, 0.0), weight)
    for term in terms:
        for synonym in _EXPANSIONS.get(term, ()):
            weight = 0.4 if synonym.isdigit() else 0.6
            weights[synonym] = max(weights.get(synonym, 0.0), weight)
    return weights


def _target_queries(question: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """Compose short FTS probes whose rank lists contribute reciprocal-rank evidence."""

    distinct = tuple(dict.fromkeys(terms))
    weights = _query_weights(distinct)
    # Rare-looking long words and explicit expansions are the best short-probe anchors.
    anchors = tuple(dict.fromkeys((*distinct, *weights)))
    anchors = tuple(term for term in anchors if len(term) > 2 and not term.isdigit())
    queries: list[str] = [" ".join(distinct[:32]) or question]
    for index in range(0, min(len(anchors), 8), 2):
        pair = anchors[index : index + 2]
        if pair:
            queries.append(" ".join(pair))
    return tuple(dict.fromkeys(query for query in queries if query.strip()))


def _location(source_id: str) -> tuple[str, int] | None:
    parts = source_id.split(_IDENTITY_SEPARATOR)
    if len(parts) != 4 or parts[0] != _PDF_PAGE_SOURCE_PREFIX:
        return None
    try:
        return unquote(parts[1]), int(parts[3])
    except ValueError:
        return None


def _score_pages(
    candidates: dict[tuple[str, str], object],
    weights: dict[str, float],
    rrf: dict[tuple[str, str], float],
    direct_terms: tuple[str, ...],
    numeric_intent: bool,
) -> list[tuple[str, str]]:
    tokenized = {key: _TOKEN_PATTERN.findall(getattr(hit, "text", "").casefold()) for key, hit in candidates.items()}
    total = len(tokenized)
    if total == 0:
        return []
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized.values():
        document_frequency.update(set(tokens))
    average_length = sum(len(tokens) for tokens in tokenized.values()) / total

    def idf(term: str) -> float:
        frequency = document_frequency.get(term, 0)
        return max(0.0, math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)))

    term_idf = {term: idf(term) for term in weights}
    bigrams = tuple(
        (left, right)
        for left, right in zip(direct_terms, direct_terms[1:], strict=False)
        if left != right
    )[:16]

    scored: list[tuple[float, tuple[str, str]]] = []
    for key, tokens in tokenized.items():
        length = len(tokens)
        counts = Counter(tokens)
        joined = " ".join(tokens)
        score = 0.0
        for term, weight in weights.items():
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            denominator = frequency + _BM25_K1 * (
                1.0 - _BM25_B + _BM25_B * length / max(average_length, 1.0)
            )
            score += weight * term_idf[term] * (frequency * (_BM25_K1 + 1.0)) / denominator
        for left, right in bigrams:
            if f"{left} {right}" in joined:
                score += 0.35 * max(term_idf.get(left, 0.0), term_idf.get(right, 0.0))
        if numeric_intent and length:
            digit_ratio = sum(1 for token in tokens if token.isdigit()) / length
            score += 1.2 * min(digit_ratio / 0.25, 1.0)
        score += 8.0 * rrf.get(key, 0.0)
        scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1][0], item[1][1]))
    return [key for _score, key in scored]


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search real Memory entries and return distinct, exactly resolvable page citations.

    Retrieval is entirely offline: short lexical probes collect real Memory hits, page text
    is reranked with corpus-IDF BM25 plus a generic financial vocabulary expansion, and the
    final citations are verified against Memory before being returned.
    """

    from powercontext.builtin.runtime import GetMemoryEntryRequest, MemoryCitation, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip() or not isinstance(limit, int) or limit < 1:
        return ()
    terms = _question_terms(question)
    weights = _query_weights(terms)
    numeric_intent = any(term in _NUMERIC_INTENT_TERMS or term.isdigit() for term in terms)

    candidates: dict[tuple[str, str], object] = {}
    rrf: dict[tuple[str, str], float] = {}

    async def collect(query: str, pool_limit: int, ranked: bool) -> None:
        try:
            page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=pool_limit))
        except Exception:  # noqa: BLE001 - an unusable probe must not fail the whole search
            return
        seen: set[tuple[str, str]] = set()
        for rank, hit in enumerate(page.hits, start=1):
            key = (hit.entry_id, hit.entry_version_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.setdefault(key, hit)
            if ranked:
                rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_CONSTANT + rank)

    for query in _CORPUS_QUERIES:
        await collect(query, _CORPUS_LIMIT, False)
    for query in _target_queries(question, terms):
        await collect(query, _TARGET_LIMIT, True)
    if not candidates:
        return ()

    ordered = _score_pages(candidates, weights, rrf, terms, numeric_intent)
    selected: list[MemoryCitation] = []
    seen_pages: set[tuple[str, int]] = set()
    for key in ordered:
        if len(selected) >= limit:
            break
        hit = candidates[key]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            response = await memory.get(GetMemoryEntryRequest(citation=citation))
            entry = getattr(response, "entry", None)
            sources = getattr(entry, "sources", ()) if entry is not None else ()
        except Exception:  # noqa: BLE001 - skip citations that no longer resolve
            continue
        if not sources or len(sources) != 1:
            continue
        location = _location(sources[0].source_id)
        if location is None or location in seen_pages:
            continue
        seen_pages.add(location)
        selected.append(citation)
    return tuple(selected)
