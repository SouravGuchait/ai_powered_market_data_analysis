"""Bounded persistence for metrics derived from canonical price history."""

from collections import deque
from decimal import ROUND_HALF_UP, Decimal
from math import sqrt

from apps.marketdata.models import IngestionJob, MetricSnapshot, PriceRecord

METRIC_BATCH_SIZE = 1000
CALCULATION_VERSION = "1"
_QUANTUM = Decimal("0.00000001")
MOVING_AVERAGE_WINDOW = 5
VOLATILITY_WINDOW = 20
TRADING_DAYS_PER_YEAR = 252


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


class MovingAverageCalculator:
    """Persist five-observation close-price SMAs for a job's accepted rows."""

    def __init__(self, window: int = MOVING_AVERAGE_WINDOW):
        if window < 1:
            raise ValueError("window must be positive")
        self.window = window

    def on_job_completed(self, *, job: IngestionJob) -> None:
        windows: dict[int, deque[Decimal]] = {}
        batch: list[MetricSnapshot] = []
        records = PriceRecord.objects.order_by("instrument_id", "trading_date", "pk")
        for record in records.iterator(chunk_size=METRIC_BATCH_SIZE):
            closes = windows.setdefault(record.instrument.pk, deque(maxlen=self.window))
            closes.append(record.close)
            if record.ingestion_job.pk == job.pk and len(closes) == self.window:
                value = (sum(closes) / Decimal(self.window)).quantize(
                    _QUANTUM, rounding=ROUND_HALF_UP
                )
                batch.append(
                    MetricSnapshot(
                        instrument_id=record.instrument.pk,
                        as_of_date=record.trading_date,
                        metric_type=MetricSnapshot.MetricType.MOVING_AVERAGE,
                        lookback_days=self.window,
                        value=value,
                        metadata={"units": "price", "window_type": "observations"},
                        calculation_version=CALCULATION_VERSION,
                    )
                )
                if len(batch) >= METRIC_BATCH_SIZE:
                    MetricSnapshot.objects.bulk_create(
                        batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
                    )
                    batch.clear()
        if batch:
            MetricSnapshot.objects.bulk_create(
                batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
            )


class RollingVolatilityCalculator:
    """Persist annualized sample volatility over valid daily returns."""

    def __init__(self, window: int = VOLATILITY_WINDOW):
        if window < 2:
            raise ValueError("window must be at least 2")
        self.window = window

    def on_job_completed(self, *, job: IngestionJob) -> None:
        previous_closes: dict[int, Decimal] = {}
        returns: dict[int, deque[Decimal]] = {}
        batch: list[MetricSnapshot] = []
        records = PriceRecord.objects.order_by("instrument_id", "trading_date", "pk")
        for record in records.iterator(chunk_size=METRIC_BATCH_SIZE):
            instrument_id = record.instrument.pk
            prior = previous_closes.get(instrument_id)
            window_returns = returns.setdefault(
                instrument_id, deque(maxlen=self.window)
            )
            if prior is not None:
                window_returns.append((record.close - prior) / prior)
                if record.ingestion_job.pk == job.pk and len(window_returns) == self.window:
                    mean = sum(window_returns) / Decimal(self.window)
                    variance = sum(
                        (value - mean) ** 2 for value in window_returns
                    ) / Decimal(self.window - 1)
                    value = (
                        Decimal(str(sqrt(float(variance))))
                        * Decimal(str(sqrt(TRADING_DAYS_PER_YEAR)))
                    ).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
                    batch.append(
                        MetricSnapshot(
                            instrument_id=instrument_id,
                            as_of_date=record.trading_date,
                            metric_type=MetricSnapshot.MetricType.ROLLING_VOLATILITY,
                            lookback_days=self.window,
                            value=value,
                            metadata={
                                "units": "annualized_fraction",
                                "window_type": "valid_observations",
                                "return_ddof": 1,
                                "annualization_days": TRADING_DAYS_PER_YEAR,
                            },
                            calculation_version=CALCULATION_VERSION,
                        )
                    )
                    if len(batch) >= METRIC_BATCH_SIZE:
                        MetricSnapshot.objects.bulk_create(
                            batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
                        )
                        batch.clear()
            previous_closes[instrument_id] = record.close
        if batch:
            MetricSnapshot.objects.bulk_create(
                batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
            )


class MaximumDrawdownCalculator:
    """Persist cumulative running maximum drawdown for this job's records."""

    def on_job_completed(self, *, job: IngestionJob) -> None:
        peaks: dict[int, Decimal] = {}
        minima: dict[int, Decimal] = {}
        batch: list[MetricSnapshot] = []
        records = (
            PriceRecord.objects.filter(ingestion_job=job)
            .select_related("instrument")
            .order_by("instrument_id", "trading_date", "pk")
        )
        for record in records.iterator(chunk_size=METRIC_BATCH_SIZE):
            instrument_id = record.instrument.pk
            close = record.close
            if close is None or close <= 0:
                continue
            peak = peaks.get(instrument_id)
            if peak is None or close > peak:
                peak = close
            drawdown = (close - peak) / peak
            peaks[instrument_id] = peak
            minimum = minima.get(instrument_id)
            if minimum is None or drawdown < minimum:
                minimum = drawdown
            minima[instrument_id] = minimum
            value = minimum.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
            batch.append(
                MetricSnapshot(
                    instrument_id=instrument_id,
                    as_of_date=record.trading_date,
                    metric_type=MetricSnapshot.MetricType.MAX_DRAWDOWN,
                    lookback_days=0,
                    value=value,
                    metadata={
                        "units": "fraction",
                        "price_basis": "close",
                        "period": "ingestion_job",
                        "window_type": "cumulative_observations",
                    },
                    calculation_version=CALCULATION_VERSION,
                )
            )
            if len(batch) >= METRIC_BATCH_SIZE:
                MetricSnapshot.objects.bulk_create(
                    batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
                )
                batch.clear()
        if batch:
            MetricSnapshot.objects.bulk_create(
                batch, batch_size=METRIC_BATCH_SIZE, ignore_conflicts=True
            )


class DefaultMetricCalculator:
    """Run the persisted metric calculators after ingestion completes."""

    def on_job_completed(self, *, job: IngestionJob) -> None:
        DailyReturnCalculator().on_job_completed(job=job)
        MovingAverageCalculator().on_job_completed(job=job)
        RollingVolatilityCalculator().on_job_completed(job=job)
        MaximumDrawdownCalculator().on_job_completed(job=job)
