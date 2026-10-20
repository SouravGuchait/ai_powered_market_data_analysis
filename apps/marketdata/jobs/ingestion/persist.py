"""Persistence for normalized ingestion output.

The database is the cross-chunk duplicate authority: every batch is committed
after `normalize_chunk` returns, so the next batch's existence query sees rows
from earlier chunks as well as rows from earlier jobs. Duplicate handling is
reject-on-conflict per DATA_MODEL.md; the first valid row in a file wins and
later copies become `ValidationIssue` rows.
"""

from collections.abc import Iterable
from datetime import date

from apps.marketdata.ingestion.schema import (
    IssueCode,
    NormalizedRecord,
    RowIssue,
)
from apps.marketdata.models import (
    Exchange,
    IngestionJob,
    Instrument,
    PriceRecord,
    ValidationIssue,
)


class InstrumentResolver:
    """Resolve `(exchange, symbol)` to instruments with a per-job cache."""

    def __init__(self) -> None:
        self._exchanges: dict[str, Exchange] = {
            exchange.code: exchange for exchange in Exchange.objects.all()
        }
        self._instruments: dict[tuple[str, str], Instrument] = {}

    def ensure_exchange(self, code: str, timezone: str) -> Exchange:
        exchange = self._exchanges.get(code)
        if exchange is None:
            exchange, _ = Exchange.objects.get_or_create(
                code=code, defaults={"name": code, "timezone": timezone}
            )
            self._exchanges[code] = exchange
        return exchange

    def timezones(self) -> dict[str, str]:
        return {
            code: exchange.timezone for code, exchange in self._exchanges.items()
        }

    def resolve(
        self, records: Iterable[NormalizedRecord]
    ) -> dict[tuple[str, str], Instrument]:
        for record in records:
            key = record.instrument_key
            if key in self._instruments:
                continue
            exchange = self._exchanges[record.exchange_code]
            instrument, _ = Instrument.objects.get_or_create(
                exchange=exchange,
                symbol=record.symbol,
                defaults={
                    "name": record.name,
                    "asset_class": record.asset_class,
                    "currency": record.currency,
                },
            )
            self._instruments[key] = instrument
        return self._instruments


def persist_records(
    job: IngestionJob,
    records: list[NormalizedRecord],
    resolver: InstrumentResolver,
    *,
    batch_size: int,
) -> tuple[int, list[RowIssue]]:
    """Insert one chunk and return `(attempted, duplicate_issues)`."""
    if not records:
        return 0, []

    instruments = resolver.resolve(records)
    pairs = [
        (instruments[record.instrument_key].pk, record) for record in records
    ]
    instrument_ids = {instrument_id for instrument_id, _ in pairs}
    dates = [record.trading_date for _, record in pairs]
    existing = set(
        PriceRecord.objects.filter(
            instrument_id__in=instrument_ids,
            trading_date__gte=min(dates),
            trading_date__lte=max(dates),
        ).values_list("instrument_id", "trading_date")
    )

    seen: set[tuple[int, date]] = set()
    issues: list[RowIssue] = []
    objects: list[PriceRecord] = []
    for instrument_id, record in pairs:
        key = (instrument_id, record.trading_date)
        if key in existing or key in seen:
            issues.append(
                RowIssue(
                    row_number=record.row_number,
                    error_code=IssueCode.DUPLICATE_RECORD,
                    field_name="timestamp",
                    message=(
                        "A record for this instrument and date already exists."
                        if key in existing
                        else "Duplicate instrument and date within the file."
                    ),
                )
            )
            continue
        seen.add(key)
        objects.append(
            PriceRecord(
                instrument_id=instrument_id,
                ingestion_job=job,
                trading_date=record.trading_date,
                open=record.open,
                high=record.high,
                low=record.low,
                close=record.close,
                adjusted_close=record.adjusted_close,
                volume=record.volume,
            )
        )

    PriceRecord.objects.bulk_create(
        objects, batch_size=batch_size, ignore_conflicts=True
    )
    return len(objects), issues


def persist_issues(
    job: IngestionJob, issues: Iterable[RowIssue], *, batch_size: int
) -> int:
    objects = [
        ValidationIssue(
            ingestion_job=job,
            row_number=issue.row_number,
            field_name=issue.field_name,
            error_code=issue.error_code,
            message=issue.message,
            raw_row=issue.raw_row,
        )
        for issue in issues
    ]
    if objects:
        ValidationIssue.objects.bulk_create(objects, batch_size=batch_size)
    return len(objects)
