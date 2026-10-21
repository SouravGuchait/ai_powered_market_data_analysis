from decimal import Decimal

import pandas as pd
import pytest

from apps.marketdata.ingestion.metrics import (
    calculate_daily_returns,
    iter_daily_returns,
)
from apps.marketdata.jobs.ingestion.metrics import DailyReturnCalculator
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


@pytest.mark.parametrize("frame", [pd.DataFrame(columns=["symbol", "timestamp", "close"]), pd.DataFrame([("A", "2026-01-01", "10")], columns=["symbol", "timestamp", "close"])])
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
