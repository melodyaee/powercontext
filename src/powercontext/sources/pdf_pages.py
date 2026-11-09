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
from collections.abc import Callable
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
_BM25_K1 = 1.5
_BM25_B = 0.5
_HEADING_WINDOW = 45
_HEADING_WEIGHT = 3.0
_NUMERIC_WEIGHT = 5.0
_DOCUMENT_WEIGHT = 6.0
_CONCEPT_WEIGHT = 0.6

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

# Generic finance concepts and the standard wording annual reports use for the
# corresponding statement or note. These are language-level vocabulary, never
# document identities, questions, answers or evidence pages.
_CONCEPT_MAP: dict[str, str] = {
    "margin": "income", "margins": "income", "profit": "income", "profitability": "income",
    "ebitda": "income", "ebit": "income", "revenue": "income", "revenues": "income",
    "sales": "income", "income": "income", "earnings": "income", "eps": "income",
    "gross": "income", "operating": "income", "unadjusted": "income",
    "ratio": "balance", "ratios": "balance", "liquidity": "balance", "quick": "balance",
    "current": "balance", "balance": "balance", "liability": "balance", "liabilities": "balance",
    "assets": "balance", "debt": "balance", "equity": "balance", "sheet": "balance",
    "largest": "balance", "healthy": "balance",
    "cash": "cashflows", "capex": "cashflows", "flows": "cashflows", "flow": "cashflows",
    "expenditures": "cashflows", "expenditure": "cashflows", "investing": "cashflows",
    "financing": "cashflows", "activities": "cashflows",
    "tax": "tax", "taxes": "tax", "rate": "tax", "rates": "tax", "effective": "tax",
    "customers": "segment", "customer": "segment", "segments": "segment", "segment": "segment",
    "geographies": "segment", "geography": "segment", "regions": "segment", "region": "segment",
    "products": "segment", "product": "segment", "categories": "segment", "category": "segment",
    "services": "segment", "geographic": "segment", "concentration": "segment",
    "acquisitions": "notes", "acquisition": "notes", "legal": "notes", "litigation": "notes",
    "lawsuits": "notes", "battles": "notes", "restructuring": "notes", "impairment": "notes",
    "cyclicality": "notes", "cyclical": "notes", "contingencies": "notes", "proceedings": "notes",
    "employees": "notes", "workforce": "notes",
}

_STATEMENT_WORDS: dict[str, tuple[str, ...]] = {
    "income": ("consolidated", "statements", "income", "operations", "revenues", "expenses", "net", "earnings"),
    "balance": ("consolidated", "balance", "sheets", "assets", "liabilities", "current", "total", "equity"),
    "cashflows": (
        "consolidated", "statements", "cash", "flows", "operating", "investing", "financing", "activities",
    ),
    "tax": ("income", "taxes", "tax", "provision", "effective", "rate", "consolidated"),
    "segment": (
        "segment", "reportable", "revenues", "business", "geographic", "notes", "data", "customers", "products",
    ),
    "notes": (
        "notes", "consolidated", "financial", "statements", "charges", "litigation", "proceedings", "acquisitions",
    ),
}


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
    concepts = {_CONCEPT_MAP[term] for term in terms if term in _CONCEPT_MAP}
    for concept in concepts:
        for word in _STATEMENT_WORDS[concept]:
            weights[word] = max(weights.get(word, 0.0), _CONCEPT_WEIGHT)
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


def _document_tokens(text: str) -> tuple[str, ...]:
    """The leading line of a projected page carries the document identity."""
    return tuple(_TOKEN_PATTERN.findall(text.split("\n", 1)[0].casefold()))


def _document_affinity(
    terms: tuple[str, ...], document_tokens: tuple[str, ...], idf: Callable[[str], float]
) -> float:
    """Generic document-name evidence shared by every page of one report."""
    affinity = 0.0
    for term in terms:
        if len(term) < 3 or term.isdigit():
            continue
        for token in document_tokens:
            if token == term or (len(token) > 4 and token.startswith(term)) or (
                len(term) > 4 and term.startswith(token)
            ):
                affinity += idf(term)
                break
    joined = "".join(term for term in terms if not term.isdigit())
    for token in document_tokens:
        if len(token) > 8 and token in joined:
            affinity += 2.0
    return affinity


def _score_pages(
    candidates: dict[tuple[str, str], object],
    weights: dict[str, float],
    direct_terms: tuple[str, ...],
) -> list[tuple[str, str]]:
    tokenized = {
        key: _TOKEN_PATTERN.findall(getattr(hit, "text", "").casefold())
        for key, hit in candidates.items()
    }
    total = len(tokenized)
    if total == 0:
        return []
    headings = {key: set(tokens[:_HEADING_WINDOW]) for key, tokens in tokenized.items()}
    document_tokens = {
        key: _document_tokens(getattr(hit, "text", "")) for key, hit in candidates.items()
    }
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized.values():
        document_frequency.update(set(tokens))
    average_length = sum(len(tokens) for tokens in tokenized.values()) / total

    def idf(term: str) -> float:
        frequency = document_frequency.get(term, 0)
        return max(0.0, math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)))

    term_idf = {term: idf(term) for term in weights}
    contributions = {term: weight * term_idf[term] for term, weight in weights.items()}
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
        score += _HEADING_WEIGHT * sum(contributions.get(term, 0.0) for term in headings[key])
        if length:
            score += _NUMERIC_WEIGHT * sum(1 for token in tokens if token.isdigit()) / length
        score += _DOCUMENT_WEIGHT * _document_affinity(direct_terms, document_tokens[key], idf)
        scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1][0], item[1][1]))
    return [key for _score, key in scored]


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search real Memory entries and return distinct, exactly resolvable page citations.

    Retrieval is entirely offline: broad lexical probes collect the persisted pages, their
    text is reranked with corpus-IDF BM25, generic financial vocabulary expansion, heading
    and document evidence, and the final citations are verified against Memory.
    """

    from powercontext.builtin.runtime import GetMemoryEntryRequest, MemoryCitation, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip() or not isinstance(limit, int) or limit < 1:
        return ()
    terms = _question_terms(question)
    weights = _query_weights(terms)

    candidates: dict[tuple[str, str], object] = {}

    async def collect(query: str, pool_limit: int) -> None:
        try:
            page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=pool_limit))
        except Exception:  # noqa: BLE001 - an unusable probe must not fail the whole search
            return
        for hit in page.hits:
            key = (hit.entry_id, hit.entry_version_id)
            candidates.setdefault(key, hit)

    for query in _CORPUS_QUERIES:
        await collect(query, _CORPUS_LIMIT)
    for query in _target_queries(question, terms):
        await collect(query, _TARGET_LIMIT)
    if not candidates:
        return ()

    ordered = _score_pages(candidates, weights, terms)
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
