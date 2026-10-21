from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.marketdata.models import (
    Exchange,
    IngestionJob,
    Instrument,
    PriceRecord,
    ValidationIssue,
)


@pytest.fixture
def owner():
    return get_user_model().objects.create_user(
        username="analytics-owner", email="analytics@example.com", password="pass12345"
    )


@pytest.fixture
def market(owner):
    exchange = Exchange.objects.create(code="XNYS", name="NYSE", timezone="UTC")
    instrument = Instrument.objects.create(
        exchange=exchange, symbol="AAA", asset_class="equity", currency="USD"
    )
    job = IngestionJob.objects.create(source_type="csv", created_by=owner)
    for idx in range(25):
        close = 100 + idx
        PriceRecord.objects.create(
            instrument=instrument,
            ingestion_job=job,
            trading_date=date(2024, 1, 1) + timedelta(days=idx),
            open=close, high=close, low=close, close=close, volume=10,
        )
    return instrument, job


@pytest.mark.django_db
def test_daily_returns_use_prior_observation_and_inclusive_range(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get("/api/v1/analytics/daily-returns/", {
        "symbol": "aaa", "exchange": "xnys", "start_date": "2024-01-03", "end_date": "2024-01-04"
    })
    assert response.status_code == 200
    assert response.data["count"] == 2
    assert [row["value"] for row in response.data["results"]] == [round(102 / 101 - 1, 8), round(103 / 102 - 1, 8)]


@pytest.mark.django_db
def test_analytics_without_filters_returns_all_owned_records(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    series = client.get("/api/v1/analytics/daily-returns/")
    assert series.status_code == 200
    assert series.data["count"] == 25
    assert series.data["symbol"] is None
    assert series.data["results"][0]["value"] is None
    assert series.data["results"][0]["symbol"] == "AAA"
    summary = client.get("/api/v1/analytics/summary/")
    assert summary.status_code == 200
    assert summary.data["price_aggregate"]["observation_count"] == 25


@pytest.mark.django_db
def test_unfiltered_series_keeps_same_symbol_exchanges_independent(owner, market):
    exchange = Exchange.objects.create(code="XNAS", name="NASDAQ", timezone="UTC")
    instrument = Instrument.objects.create(
        exchange=exchange, symbol="AAA", asset_class="equity", currency="USD"
    )
    job = IngestionJob.objects.create(source_type="csv", created_by=owner)
    for day, close in ((date(2024, 1, 1), 50), (date(2024, 1, 2), 55)):
        PriceRecord.objects.create(
            instrument=instrument, ingestion_job=job, trading_date=day,
            open=close, high=close, low=close, close=close, volume=1,
        )
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get("/api/v1/analytics/daily-returns/")
    assert response.status_code == 200
    row = next(
        item for item in response.data["results"]
        if item["date"].isoformat() == "2024-01-02" and item["exchange"] == "XNAS"
    )
    assert row["value"] == 0.1


@pytest.mark.django_db
def test_analytics_validates_ranges_windows_and_unknown_instruments(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    base = "/api/v1/analytics/moving-averages/"
    common = {"symbol": "AAA", "start_date": "2024-01-02", "end_date": "2024-01-05"}
    assert client.get(base, common | {"window": "9"}).status_code == 400
    assert client.get(base, common | {"window": "5", "page_size": "101"}).status_code == 400
    assert client.get(base, common | {"start_date": "2024-01-06"}).status_code == 400
    assert client.get(base, {**common, "symbol": "NOPE"}).status_code == 400


@pytest.mark.django_db
def test_ambiguous_symbol_requires_exchange(owner, market):
    exchange = Exchange.objects.create(code="XNAS", name="NASDAQ", timezone="UTC")
    instrument = Instrument.objects.create(
        exchange=exchange, symbol="AAA", asset_class="equity", currency="USD"
    )
    job = IngestionJob.objects.create(source_type="csv", created_by=owner)
    PriceRecord.objects.create(
        instrument=instrument, ingestion_job=job, trading_date="2024-01-01",
        open=10, high=10, low=10, close=10, volume=1,
    )
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get("/api/v1/analytics/summary/", {
        "symbol": "AAA", "start_date": "2024-01-01", "end_date": "2024-01-10",
    })
    assert response.status_code == 400
    assert "exchange" in response.data


@pytest.mark.django_db
def test_analytics_requires_auth_and_is_owner_scoped(owner, market):
    url = "/api/v1/analytics/summary/?symbol=AAA&start_date=2024-01-01&end_date=2024-01-25"
    assert APIClient().get(url).status_code == 401
    stranger = get_user_model().objects.create_user(
        username="analytics-stranger", email="stranger@example.com", password="pass12345"
    )
    client = APIClient()
    client.force_authenticate(stranger)
    assert client.get(url).status_code == 400
    assert client.get("/api/v1/analytics/daily-returns/").data["count"] == 0
    assert client.get("/api/v1/analytics/summary/").data["price_aggregate"]["observation_count"] == 0


@pytest.mark.django_db
def test_moving_average_uses_preceding_window_and_paginates(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get("/api/v1/analytics/moving-averages/", {
        "symbol": "AAA", "start_date": "2024-01-06", "end_date": "2024-01-07",
        "window": "5", "page_size": "1",
    })
    assert response.status_code == 200
    assert response.data["count"] == 2
    assert response.data["window"] == 5
    assert response.data["results"][0]["value"] == 103.0


@pytest.mark.django_db
def test_rolling_volatility_and_drawdown_api_conventions(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    volatility = client.get("/api/v1/analytics/rolling-volatility/", {
        "symbol": "AAA", "start_date": "2024-01-22", "end_date": "2024-01-25",
        "window": "20",
    })
    assert volatility.status_code == 200
    assert volatility.data["annualized"] is True
    assert volatility.data["return_ddof"] == 1
    assert all(row["value"] is not None for row in volatility.data["results"])
    drawdown = client.get("/api/v1/analytics/drawdown/", {
        "symbol": "AAA", "start_date": "2024-01-01", "end_date": "2024-01-03",
    })
    assert drawdown.status_code == 200
    assert drawdown.data["running_peak_policy"] == "requested_period"
    assert [row["value"] for row in drawdown.data["results"]] == [0, 0, 0]


@pytest.mark.django_db
def test_job_issues_are_owner_scoped_and_paginated(owner, market):
    _, job = market
    ValidationIssue.objects.create(ingestion_job=job, row_number=4, error_code="bad", message="Bad row")
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get(f"/api/v1/jobs/{job.pk}/issues/?issue_code=bad&page_size=1")
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["error_code"] == "bad"
    stranger = get_user_model().objects.create_user(
        username="issue-stranger", email="issue-stranger@example.com", password="pass12345"
    )
    client.force_authenticate(stranger)
    assert client.get(f"/api/v1/jobs/{job.pk}/issues/").status_code == 404
