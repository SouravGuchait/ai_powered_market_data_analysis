"""Bounded persistence for metrics derived from canonical price history."""

from decimal import ROUND_HALF_UP, Decimal

from apps.marketdata.models import IngestionJob, MetricSnapshot, PriceRecord

METRIC_BATCH_SIZE = 1000
CALCULATION_VERSION = "1"
_QUANTUM = Decimal("0.00000001")


class DailyReturnCalculator:
    """Persist daily fractional returns for a job's accepted price records."""

    def on_job_completed(self, *, job: IngestionJob) -> None:
        previous: dict[int, Decimal] = {}
        batch: list[MetricSnapshot] = []
        records = PriceRecord.objects.select_related("instrument").order_by(
            "instrument_id", "trading_date", "pk"
        )
        for record in records.iterator(chunk_size=METRIC_BATCH_SIZE):
            instrument_id = record.instrument.pk
            prior = previous.get(instrument_id)
            if record.ingestion_job.pk == job.pk and prior is not None and prior > 0:
                value = ((record.close - prior) / prior).quantize(
                    _QUANTUM, rounding=ROUND_HALF_UP
                )
                batch.append(
                    MetricSnapshot(
                        instrument_id=instrument_id,
                        as_of_date=record.trading_date,
                        metric_type=MetricSnapshot.MetricType.DAILY_RETURN,
                        lookback_days=0,
                        value=value,
                        calculation_version=CALCULATION_VERSION,
                    )
                )
                if len(batch) >= METRIC_BATCH_SIZE:
                    MetricSnapshot.objects.bulk_create(
                        batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
                    )
                    batch.clear()
            if record.close > 0:
                previous[instrument_id] = record.close

        if batch:
            MetricSnapshot.objects.bulk_create(
                batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
            )
