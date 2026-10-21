from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.marketdata.models import (
    Exchange,
    IngestionJob,
    Instrument,
    PriceRecord,
    ValidationIssue,
)


@pytest.fixture(autouse=True)
def clear_analytics_cache():
    cache.clear()
    yield
    cache.clear()


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
    assert "count" not in response.data
    assert "next" not in response.data
    assert "previous" not in response.data
    assert len(response.data["results"]) == 2
    assert [row["value"] for row in response.data["results"]] == [round(102 / 101 - 1, 8), round(103 / 102 - 1, 8)]
    assert response.data["results"][0]["date"].isoformat() == "2024-01-03"


@pytest.mark.django_db
def test_daily_returns_skip_invalid_latest_prior_close(owner, market):
    instrument, job = market
    PriceRecord.objects.create(
        instrument=instrument, ingestion_job=job, trading_date="2023-12-31",
        open=90, high=90, low=90, close=90, volume=1,
    )
    client = APIClient()
    client.force_authenticate(owner)
    response = client.get("/api/v1/analytics/daily-returns/", {
        "symbol": "AAA", "exchange": "XNYS", "start_date": "2024-01-01", "end_date": "2024-01-01",
    })
    assert response.status_code == 200
    assert response.data["results"][0]["value"] == round(100 / 90 - 1, 8)


@pytest.mark.django_db
def test_analytics_without_filters_returns_all_owned_records(owner, market):
    client = APIClient()
    client.force_authenticate(owner)
    series = client.get("/api/v1/analytics/daily-returns/")
    assert series.status_code == 200
    assert len(series.data["results"]) == 25
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
    assert client.get("/api/v1/analytics/daily-returns/").data["results"] == []
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
    assert len(response.data["results"]) == 2
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


@pytest.mark.django_db
def test_analytics_cache_hits_and_scopes_by_user_and_filters(owner, market, monkeypatch):
    import apps.analytics.api as analytics_api

    calls = 0
    build = analytics_api._build_summary

    def counted(*args):
        nonlocal calls
        calls += 1
        return build(*args)

    monkeypatch.setattr(analytics_api, "_build_summary", counted)
    client = APIClient()
    client.force_authenticate(owner)
    params = {"symbol": " aaa ", "exchange": "xnys", "start_date": "2024-01-01", "end_date": "2024-01-25"}
    assert client.get("/api/v1/analytics/summary/", params).status_code == 200
    assert client.get("/api/v1/analytics/summary/", params).status_code == 200
    assert calls == 1
    changed = params | {"end_date": "2024-01-24"}
    client.get("/api/v1/analytics/summary/", changed)
    assert calls == 2
    stranger = get_user_model().objects.create_user(
        username="analytics-cache-stranger", email="cache-stranger@example.com", password="pass12345"
    )
    client.force_authenticate(stranger)
    assert client.get("/api/v1/analytics/summary/", params).status_code == 400
    assert calls == 3  # the stranger's permission failure is evaluated, never cached


@pytest.mark.django_db
def test_analytics_cache_invalidation_and_backend_failure_fallback(owner, market, monkeypatch):
    import apps.analytics.api as analytics_api
    import apps.analytics.cache as analytics_cache

    client = APIClient()
    client.force_authenticate(owner)
    params = {"symbol": "AAA", "exchange": "XNYS", "start_date": "2024-01-01", "end_date": "2024-01-25"}
    url = "/api/v1/analytics/summary/"
    original = analytics_api._build_summary
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(analytics_api, "_build_summary", counted)
    client.get(url, params)
    analytics_cache.invalidate_owner_analytics(owner.pk)
    client.get(url, params)
    assert calls == 2

    class UnavailableCache:
        def get(self, *args, **kwargs):
            raise OSError("cache unavailable")

        def set(self, *args, **kwargs):
            raise OSError("cache unavailable")

    monkeypatch.setattr(analytics_cache, "cache", UnavailableCache())
    response = client.get(url, params)
    assert response.status_code == 200
    assert response.data["price_aggregate"]["observation_count"] == 25


def test_analytics_cache_is_separate_by_user_and_filters():
    from apps.analytics.cache import analytics_response

    first = analytics_response(101, "summary", {"symbol": "AAA"}, lambda: Response({"user": 101}))
    same = analytics_response(101, "summary", {"symbol": "AAA"}, lambda: Response({"user": -1}))
    other_user = analytics_response(102, "summary", {"symbol": "AAA"}, lambda: Response({"user": 102}))
    other_filter = analytics_response(101, "summary", {"symbol": "BBB"}, lambda: Response({"user": 101}))
    assert first.data == same.data == {"user": 101}
    assert other_user.data == {"user": 102}
    assert other_filter.data == {"user": 101}
