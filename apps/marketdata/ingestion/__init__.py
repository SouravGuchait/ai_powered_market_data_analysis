"""Format-independent parsing, validation, and normalization for ingestion.

This package has no dependency on Django request objects and does not import
the ORM. The worker-facing persistence layer lives in
`apps.marketdata.jobs.ingestion`.
"""

from apps.marketdata.ingestion.normalize import normalize_chunk
from apps.marketdata.ingestion.parsing import iter_input_chunks
from apps.marketdata.ingestion.schema import (
    InputError,
    IssueCode,
    NormalizedChunk,
    RowIssue,
)

__all__ = [
    "InputError",
    "IssueCode",
    "NormalizedChunk",
    "RowIssue",
    "iter_input_chunks",
    "normalize_chunk",
]
