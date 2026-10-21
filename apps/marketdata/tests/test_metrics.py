from decimal import Decimal

import pandas as pd
import pytest

from apps.marketdata.ingestion.metrics import (
    calculate_daily_returns,
    calculate_drawdowns,
    calculate_moving_average,
    iter_daily_returns,
    iter_drawdowns,
    iter_moving_averages,
)
from apps.marketdata.jobs.ingestion.metrics import (
    DailyReturnCalculator,
    MaximumDrawdownCalculator,
    MovingAverageCalculator,
)
from apps.marketdata.models import (
    Exchange,
    IngestionJob,
    Instrument,
    MetricSnapshot,
    PriceRecord,
)


@pytest.fixture
def instrument():
    exchange = Exchange.objects.create(code="XNYS", name="NYSE", timezone="UTC")
    return Instrument.objects.create(
        exchange=exchange, symbol="AAA", asset_class="equity", currency="USD"
    )


@pytest.fixture
def job():
    return IngestionJob.objects.create(source_type="csv")


def test_daily_returns_are_sorted_grouped_and_fractional():
    frame = pd.DataFrame(
        [
            ("AAA", "2026-01-03", "9"),
            ("BBB", "2026-01-02", "20"),
            ("AAA", "2026-01-01", "10"),
            ("BBB", "2026-01-01", "10"),
        ],
        columns=["symbol", "timestamp", "close"],
    )
    returns, _ = calculate_daily_returns(frame)
    assert returns.iloc[0] == -0.1
    assert returns.iloc[1] == 1.0
    assert pd.isna(returns.iloc[2])
    assert pd.isna(returns.iloc[3])


def test_daily_returns_carry_state_across_chunks_and_skip_invalid_close():
    chunks = [
        pd.DataFrame(
            [("AAA", "2026-01-01", "10"), ("AAA", "2026-01-02", None)],
            columns=["symbol", "timestamp", "close"],
        ),
        pd.DataFrame(
            [("AAA", "2026-01-03", "12"), ("AAA", "2026-01-04", "6")],
            columns=["symbol", "timestamp", "close"],
        ),
    ]
    result = list(iter_daily_returns(chunks))
    assert pd.isna(result[0][1].iloc[0])
    assert pd.isna(result[0][1].iloc[1])
    assert result[1][1].tolist() == [0.2, -0.5]


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame(columns=["symbol", "timestamp", "close"]),
        pd.DataFrame(
            [("A", "2026-01-01", "10")], columns=["symbol", "timestamp", "close"]
        ),
    ],
)
def test_empty_and_single_observation_have_no_return(frame):
    returns, state = calculate_daily_returns(frame)
    assert returns.isna().all()
    assert len(state) == len(frame)


def test_duplicate_symbol_timestamp_is_rejected():
    frame = pd.DataFrame(
        [("AAA", "2026-01-01", "10"), ("AAA", "2026-01-01", "11")],
        columns=["symbol", "timestamp", "close"],
    )
    with pytest.raises(ValueError, match="duplicate"):
        calculate_daily_returns(frame)


def test_invalid_or_nonpositive_prior_close_does_not_create_return():
    frame = pd.DataFrame(
        [("AAA", "2026-01-02", "10")],
        columns=["symbol", "timestamp", "close"],
    )
    returns, _ = calculate_daily_returns(frame, {"AAA": Decimal(0)})
    assert pd.isna(returns.iloc[0])


def test_moving_average_full_window_rollover_and_unsorted_rows():
    frame = pd.DataFrame(
        [("A", f"2026-01-0{i}", str(i)) for i in (5, 1, 4, 2, 3, 6)],
        columns=["symbol", "timestamp", "close"],
    )
    values, _ = calculate_moving_average(frame, window=3)
    assert values.iloc[0] == 4.0
    assert values.iloc[4] == 2.0
    assert pd.isna(values.iloc[3])
    assert values.iloc[2] == 3.0
    assert values.iloc[5] == 5.0


def test_moving_average_chunk_state_symbols_invalids_and_boundaries():
    chunks = [
        pd.DataFrame(
            [("A", "1", "1"), ("A", "2", None), ("B", "1", "10")],
            columns=["symbol", "timestamp", "close"],
        ),
        pd.DataFrame(
            [("A", "3", "2"), ("A", "4", "3"), ("A", "5", "4")],
            columns=["symbol", "timestamp", "close"],
        ),
    ]
    result = list(iter_moving_averages(chunks, window=3))
    assert result[0][1].isna().all()
    assert pd.isna(result[1][1].iloc[0])
    assert result[1][1].iloc[1] == 2.0
    assert result[1][1].iloc[2] == 3.0
    one, _ = calculate_moving_average(chunks[0], window=1)
    assert one.iloc[0] == 1.0 and pd.isna(one.iloc[1]) and one.iloc[2] == 10.0
    with pytest.raises(ValueError, match="positive"):
        calculate_moving_average(chunks[0], window=0)


def test_moving_average_insufficient_empty_and_duplicate_observations():
    empty = pd.DataFrame(columns=["symbol", "timestamp", "close"])
    values, state = calculate_moving_average(empty)
    assert values.empty and state == {}
    short = pd.DataFrame(
        [("A", "2026-01-01", "1"), ("A", "2026-01-02", "2")],
        columns=["symbol", "timestamp", "close"],
    )
    values, _ = calculate_moving_average(short)
    assert values.isna().all()
    repeated = pd.concat([short, short.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        calculate_moving_average(repeated)


def test_drawdowns_track_peak_trough_recovery_and_new_high():
    frame = pd.DataFrame(
        [("A", f"2026-01-0{i}", str(close)) for i, close in enumerate((100, 80, 90, 110, 55), 1)],
        columns=["symbol", "timestamp", "close"],
    )
    values, peaks, minima = calculate_drawdowns(frame)
    assert values.tolist() == [0.0, -0.2, -0.2, -0.2, -0.5]
    assert peaks == {"A": Decimal(110)}
    assert minima == {"A": Decimal("-0.5")}


def test_drawdown_symbols_invalids_chunks_empty_and_period_reset():
    chunks = [
        pd.DataFrame(
            [("A", "1", "100"), ("B", "1", "20"), ("A", "2", None)],
            columns=["symbol", "timestamp", "close"],
        ),
        pd.DataFrame(
            [("A", "3", "50"), ("B", "2", "20"), ("B", "3", "25")],
            columns=["symbol", "timestamp", "close"],
        ),
    ]
    result = list(iter_drawdowns(chunks))
    assert result[0][1].iloc[:2].tolist() == [0.0, 0.0]
    assert pd.isna(result[0][1].iloc[2])
    assert result[1][1].tolist() == [-0.5, 0.0, 0.0]
    fresh, _, _ = calculate_drawdowns(chunks[1])
    assert fresh.tolist() == [0.0, 0.0, 0.0]
    empty, peaks, minima = calculate_drawdowns(
        pd.DataFrame(columns=["symbol", "timestamp", "close"])
    )
    assert empty.empty and peaks == minima == {}
    repeated = pd.concat([chunks[0].iloc[[0]], chunks[0].iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        calculate_drawdowns(repeated)


@pytest.mark.django_db
def test_drawdown_persists_job_period_only_and_is_repeatable(instrument, job):
    for day, close in enumerate((100, 50), start=1):
        PriceRecord.objects.create(
            instrument=instrument, ingestion_job=job,
            trading_date=f"2024-01-0{day}", open=close, high=close,
            low=close, close=close, volume=0,
        )
    current_job = IngestionJob.objects.create(source_type="csv")
    for day, close in ((3, 80), (4, 100), (5, 60)):
        PriceRecord.objects.create(
            instrument=instrument, ingestion_job=current_job,
            trading_date=f"2024-01-0{day}", open=close, high=close,
            low=close, close=close, volume=0,
        )

    calculator = MaximumDrawdownCalculator()
    calculator.on_job_completed(job=current_job)
    calculator.on_job_completed(job=current_job)
    snapshots = list(MetricSnapshot.objects.filter(
        instrument=instrument, metric_type=MetricSnapshot.MetricType.MAX_DRAWDOWN
    ).order_by("as_of_date"))
    assert [snapshot.value for snapshot in snapshots] == [
        Decimal("0E-8"), Decimal("0E-8"), Decimal("-0.40000000")
    ]
    assert snapshots[-1].metadata == {
        "units": "fraction", "price_basis": "close", "period": "ingestion_job",
        "window_type": "cumulative_observations",
    }


@pytest.mark.django_db
def test_moving_average_persists_only_completed_job_rows(instrument, job):
    for day, close in enumerate((10, 20, 30), start=1):
        PriceRecord.objects.create(
            instrument=instrument,
            ingestion_job=job,
            trading_date=f"2024-01-0{day}",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0,
        )
    current_job = IngestionJob.objects.create(source_type="csv")
    for day, close in ((4, 40), (5, 50)):
        PriceRecord.objects.create(
            instrument=instrument,
            ingestion_job=current_job,
            trading_date=f"2024-01-0{day}",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0,
        )

    MovingAverageCalculator(window=5).on_job_completed(job=current_job)
    snapshot = MetricSnapshot.objects.get(
        instrument=instrument,
        as_of_date="2024-01-05",
        metric_type=MetricSnapshot.MetricType.MOVING_AVERAGE,
        lookback_days=5,
    )
    assert snapshot.value == Decimal("30.00000000")
    assert snapshot.metadata == {"units": "price", "window_type": "observations"}
    assert (
        MetricSnapshot.objects.filter(
            instrument=instrument, metric_type=MetricSnapshot.MetricType.MOVING_AVERAGE
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_calculator_persists_job_returns_using_prior_history(instrument, job):
    PriceRecord.objects.create(
        instrument=instrument,
        ingestion_job=job,
        trading_date="2024-01-01",
        open=100,
        high=100,
        low=100,
        close=100,
        volume=0,
    )
    current_job = IngestionJob.objects.create(source_type="csv")
    PriceRecord.objects.create(
        instrument=instrument,
        ingestion_job=current_job,
        trading_date="2024-01-02",
        open=110,
        high=110,
        low=110,
        close=110,
        volume=0,
    )

    DailyReturnCalculator().on_job_completed(job=current_job)

    snapshot = MetricSnapshot.objects.get(
        instrument=instrument,
        as_of_date="2024-01-02",
        metric_type=MetricSnapshot.MetricType.DAILY_RETURN,
    )
    assert snapshot.value == Decimal("0.10000000")
