from decimal import Decimal

import pandas as pd
import pytest

from apps.marketdata.ingestion.metrics import (
    calculate_daily_returns,
    calculate_moving_average,
    calculate_rolling_volatility,
    iter_daily_returns,
    iter_moving_averages,
    iter_rolling_volatility,
)
from apps.marketdata.jobs.ingestion.metrics import (
    DailyReturnCalculator,
    MovingAverageCalculator,
    RollingVolatilityCalculator,
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


def test_rolling_volatility_uses_sample_std_and_annualizes():
    frame = pd.DataFrame(
        [("A", f"2026-01-0{day}", close) for day, close in
         ((1, "100"), (2, "110"), (3, "100"), (4, "120"))],
        columns=["symbol", "timestamp", "close"],
    )
    values, _ = calculate_rolling_volatility(frame, window=3)
    expected = pd.Series([0.1, -1 / 11, 0.2]).std(ddof=1) * (252**0.5)
    assert pd.isna(values.iloc[:3]).all()
    assert values.iloc[3] == pytest.approx(expected)


def test_rolling_volatility_state_is_independent_per_symbol_and_ignores_zero_prior():
    frame = pd.DataFrame(
        [("A", "1", "10"), ("B", "1", "20"), ("A", "2", "11"),
         ("B", "2", "18"), ("A", "3", "12.1"), ("B", "3", "16.2")],
        columns=["symbol", "timestamp", "close"],
    )
    values, _ = calculate_rolling_volatility(frame, window=2)
    assert pd.isna(values.iloc[:4]).all()
    assert values.iloc[4] == pytest.approx(0.0)
    assert values.iloc[5] == pytest.approx(0.0, abs=1e-7)
    zero_prior = pd.DataFrame(
        [("A", "4", "13")], columns=["symbol", "timestamp", "close"]
    )
    result, _ = calculate_rolling_volatility(
        zero_prior, window=2, previous_state={"A": (Decimal(0), [])}
    )
    assert result.isna().all()


def test_rolling_volatility_constant_returns_and_chunk_boundaries():
    chunks = [
        pd.DataFrame(
            [("A", "1", "100"), ("A", "2", "110")],
            columns=["symbol", "timestamp", "close"],
        ),
        pd.DataFrame(
            [("A", "3", "121"), ("A", "4", "133.1")],
            columns=["symbol", "timestamp", "close"],
        ),
    ]
    results = list(iter_rolling_volatility(chunks, window=2))
    assert results[0][1].isna().all()
    assert results[1][1].iloc[0] == pytest.approx(0.0)
    assert results[1][1].iloc[1] == pytest.approx(0.0, abs=1e-7)
    whole, _ = calculate_rolling_volatility(pd.concat(chunks), window=2)
    assert pd.concat([item[1] for item in results]).tolist() == pytest.approx(
        whole.tolist(), nan_ok=True
    )


def test_rolling_volatility_skips_invalid_closes_and_handles_empty_and_duplicates():
    empty = pd.DataFrame(columns=["symbol", "timestamp", "close"])
    values, state = calculate_rolling_volatility(empty, window=2)
    assert values.empty and state == {}
    frame = pd.DataFrame(
        [("A", "1", "100"), ("A", "2", None), ("A", "3", "110")],
        columns=["symbol", "timestamp", "close"],
    )
    values, _ = calculate_rolling_volatility(frame, window=2)
    assert values.isna().all()
    one, _ = calculate_rolling_volatility(frame.iloc[:1], window=2)
    assert one.isna().all()
    repeated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        calculate_rolling_volatility(repeated, window=2)
    with pytest.raises(ValueError, match="at least 2"):
        calculate_rolling_volatility(frame, window=1)


@pytest.mark.django_db
def test_rolling_volatility_persists_full_history_but_only_current_job_rows(instrument, job):
    for day, close in enumerate((100, 110, 100, 120), start=1):
        PriceRecord.objects.create(
            instrument=instrument, ingestion_job=job,
            trading_date=f"2024-01-{day:02d}", open=close, high=close,
            low=close, close=close, volume=0,
        )
    current_job = IngestionJob.objects.create(source_type="csv")
    for day, close in ((5, 130), (6, 125)):
        PriceRecord.objects.create(
            instrument=instrument, ingestion_job=current_job,
            trading_date=f"2024-01-{day:02d}", open=close, high=close,
            low=close, close=close, volume=0,
        )
    RollingVolatilityCalculator(window=3).on_job_completed(job=current_job)
    snapshots = MetricSnapshot.objects.filter(
        instrument=instrument,
        metric_type=MetricSnapshot.MetricType.ROLLING_VOLATILITY,
    ).order_by("as_of_date")
    assert [day.isoformat() for day in snapshots.values_list("as_of_date", flat=True)] == [
        "2024-01-05", "2024-01-06"
    ]
    assert snapshots.first().lookback_days == 3
    assert snapshots.first().metadata == {
        "units": "annualized_fraction",
        "window_type": "valid_observations",
        "return_ddof": 1,
        "annualization_days": 252,
    }
