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

"""Row-level CSV capture so retrieval citations resolve to one exact record."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    SourceDefinition,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME = "csv-row"
_CSV_ROW_SOURCE_PREFIX = "csv"
_IDENTITY_SEPARATOR = ":"


class CSVDocumentInput(BaseModel):
    """One offline CSV document to be split into row-level Sources."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One validated CSV record addressed by its document and primary key value."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV record that keeps its header order and raw field values."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the stable, delimiter-safe identity of one CSV record."""

    _require_document_id(document_id)
    identity = _IDENTITY_SEPARATOR.join(
        (
            _CSV_ROW_SOURCE_PREFIX,
            quote(document_id, safe=""),
            quote(primary_key_value, safe=""),
        )
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"CSV row identity must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate one whole CSV document atomically into row inputs."""

    _require_document_id(value.document_id)
    if not isinstance(value.primary_key, str) or not value.primary_key:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    records = _read_records(value.content)
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003

    columns = records[0]
    _validate_columns(columns)
    try:
        primary_key_index = columns.index(value.primary_key)
    except ValueError:
        raise ValueError(  # noqa: TRY003
            f"primary_key {value.primary_key!r} does not exactly match a CSV column"
        ) from None

    width = len(columns)
    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != width:
            raise ValueError(  # noqa: TRY003
                f"CSV record width {len(record)} does not match header width {width}"
            )
        primary_key_value = record[primary_key_index]
        if not primary_key_value.strip():
            raise ValueError("CSV primary key value must not be blank")  # noqa: TRY003
        if primary_key_value in seen_keys:
            raise ValueError(  # noqa: TRY003
                f"CSV primary key value {primary_key_value!r} is not unique in the document"
            )
        seen_keys.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=tuple(columns),
                values=tuple(record),
            )
        )
    return tuple(rows)


def _require_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003


def _read_records(content: bytes) -> list[list[str]]:
    if not isinstance(content, bytes):
        raise ValueError("CSV content must be raw bytes")  # noqa: TRY003
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return [list(record) for record in reader]
    except csv.Error as error:
        raise ValueError(f"CSV content is malformed: {error}") from error  # noqa: TRY003


def _validate_columns(columns: list[str]) -> None:
    if not columns:
        raise ValueError("CSV document must contain a non-empty header record")  # noqa: TRY003
    seen: set[str] = set()
    for column in columns:
        if not column.strip():
            raise ValueError("CSV header columns must not be blank")  # noqa: TRY003
        if column in seen:
            raise ValueError(f"CSV header column {column!r} is not unique")  # noqa: TRY003
        seen.add(column)


class _CSVRowSourceAdapter:
    """Resolve CSV row inputs into visible Sources and read them back losslessly."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    """Expose one CSV record as canonical text evidence without losing column order."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        mapping = {column: row[column] for column in source.columns}
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": mapping,
            },
        )


_CSV_ROW_SOURCE_ADAPTER = _CSVRowSourceAdapter()

CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    _CSV_ROW_SOURCE_ADAPTER,
    version="1",
    projections=(_CSVRowTextEvidenceProjection(),),
)
