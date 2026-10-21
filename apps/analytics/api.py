"""Bounded, owner-scoped analytical API helpers."""

import logging
import time
from typing import Any

import pandas as pd
from django.db.models import Avg, Count, Max, Min
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.serializers import CharField, DateField, IntegerField, Serializer

from apps.marketdata.ingestion.metrics import (
    calculate_daily_returns,
    calculate_drawdowns,
    calculate_moving_average,
    calculate_rolling_volatility,
)
from apps.marketdata.models import Instrument, PriceRecord, ValidationIssue

logger = logging.getLogger(__name__)
MAX_ANALYSIS_DAYS = 3660
SUPPORTED_WINDOWS = {5, 20}


class AnalyticsQuery(Serializer):
    symbol = CharField(required=True, max_length=32)
    exchange = CharField(required=False, max_length=16)
    start_date = DateField(required=False)
    end_date = DateField(required=False)
    page = IntegerField(required=False, min_value=1)
    page_size = IntegerField(required=False, min_value=1, max_value=100)
    ordering = CharField(required=False)
    window = IntegerField(required=False, min_value=1)


class IssueQuery(Serializer):
    page = IntegerField(required=False, min_value=1)
    page_size = IntegerField(required=False, min_value=1, max_value=100)
    issue_code = CharField(required=False, max_length=64)
    ordering = CharField(required=False)


class AnalyticsPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


def _request_data(request, *, window=False):
    allowed = {"symbol", "exchange", "start_date", "end_date", "page", "page_size", "ordering"}
    serializer = AnalyticsQuery(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    data: dict[Any, Any] | Any = serializer.validated_data
    if window:
        allowed.add("window")
    extra = set(request.query_params) - allowed
    if extra:
        raise ValidationError({"query": f"Unsupported parameter(s): {', '.join(sorted(extra))}."})   
    data["symbol"] = data["symbol"].strip().upper()
    if data.get("exchange"):
        data["exchange"] = data["exchange"].strip().upper()
    if data.get("start_date") and data.get("end_date") and data["start_date"] > data["end_date"]:
        raise ValidationError({"end_date": "Must be on or after start_date."})
    if data.get("window") and data["window"] not in SUPPORTED_WINDOWS:
        raise ValidationError({"window": "Supported windows are 5 and 20."})
    if data.get("ordering", "trading_date") not in {"trading_date", "-trading_date"}:
        raise ValidationError({"ordering": "Supported ordering is trading_date or -trading_date."})
    if not data.get("start_date") or not data.get("end_date"):
        raise ValidationError({"start_date": "Both start_date and end_date are required."})
    if (data["end_date"] - data["start_date"]).days > MAX_ANALYSIS_DAYS:
        raise ValidationError({"date_range": f"Date range cannot exceed {MAX_ANALYSIS_DAYS} days."})
    return data


def _owned_records(user, data, *, context_days=0):
    instruments = Instrument.objects.filter(
        symbol=data["symbol"],
        price_records__ingestion_job__created_by=user,
    ).distinct()
    if data.get("exchange"):
        instruments = instruments.filter(exchange__code=data["exchange"])
    ids = list(instruments.values_list("id", flat=True)[:2])
    if not ids:
        raise ValidationError({"symbol": "No accessible instrument matches the requested symbol and exchange."})
    if len(ids) > 1:
        raise ValidationError({"exchange": "This symbol exists on multiple exchanges; specify exchange."})
    records = PriceRecord.objects.filter(instrument_id=ids[0], ingestion_job__created_by=user)
    preceding = list(records.filter(trading_date__lt=data["start_date"]).order_by("-trading_date", "-pk")[:context_days])
    scoped = records.filter(trading_date__gte=data["start_date"], trading_date__lte=data["end_date"])
    context_ids = [row.pk for row in preceding]
    scoped = PriceRecord.objects.filter(pk__in=context_ids) | scoped
    return scoped.order_by("trading_date", "pk"), instruments.select_related("exchange").get(pk=ids[0])


def _series(request, metric):
    started = time.monotonic()
    data = _request_data(request, window=metric in {"moving_average", "rolling_volatility"})
    paginator = AnalyticsPagination()
    window = data.get("window", 5 if metric == "moving_average" else 20)
    context = window if metric == "moving_average" else window + 1 if metric == "rolling_volatility" else 1 if metric == "daily_return" else 0
    records , instrument = _owned_records(request.user, data, context_days=context)
    frame = pd.DataFrame.from_records(records.values("trading_date", "close"))
    if not frame.empty:
        frame = frame.rename(columns={"trading_date": "timestamp"})
        frame.insert(0, "symbol", instrument.symbol)
    if metric == "daily_return":
        values, _ = calculate_daily_returns(frame)
        metric_type, units = "daily_return", "fraction"
    elif metric == "moving_average":
        values, _ = calculate_moving_average(frame, window=window)
        metric_type, units = "moving_average", "price"
    elif metric == "rolling_volatility":
        values, _ = calculate_rolling_volatility(frame, window=window)
        metric_type, units = "rolling_volatility", "annualized_fraction"
    else:
        values, _, _ = calculate_drawdowns(frame)
        metric_type, units = "max_drawdown", "fraction"
    rows = []
    for pos, record in enumerate(frame.itertuples(index=False)):
        day = record.timestamp
        if data["start_date"] <= day <= data["end_date"]:
            value = values.iloc[pos]
            rows.append({"date": day, "value": None if pd.isna(value) else round(float(value), 8)})
    if data.get("ordering") == "-trading_date":
        rows.reverse()

    page = paginator.paginate_queryset(rows, request)
    response = paginator.get_paginated_response(data=page)
    logger.info("analytics.request metric=%s instrument=%s range=%s..%s results=%s duration_ms=%.1f", metric_type, instrument.pk, data["start_date"], data["end_date"], len(rows), (time.monotonic()-started)*1000)
    response.data = {}
    response.data.update({"symbol": instrument.symbol, "exchange": instrument.exchange.code, "start_date": data["start_date"], "end_date": data["end_date"], "metric": metric_type, "units": units})
    if metric in {"moving_average", "rolling_volatility"}:
        response.data["window"] = window
    if metric == "rolling_volatility":
        response.data.update({"annualized": True, "annualization_days": 252, "return_ddof": 1})
    if metric == "drawdown":
        response.data["running_peak_policy"] = "requested_period"
    return response


def summary(request):
    data = _request_data(request)
    records, instrument = _owned_records(request.user, data)
    stats = records.aggregate(count=Count("pk"), minimum=Min("close"), maximum=Max("close"), average=Avg("close"))
    return {"symbol": instrument.symbol, "exchange": instrument.exchange.code, "start_date": data["start_date"], "end_date": data["end_date"], "price_aggregate": {"observation_count": stats["count"], "minimum_close": stats["minimum"], "maximum_close": stats["maximum"], "average_close": stats["average"]}}


def issues(request, job):
    allowed = {"page", "page_size", "issue_code", "ordering"}
    extra = set(request.query_params) - allowed
    if extra:
        raise ValidationError({"query": f"Unsupported parameter(s): {', '.join(sorted(extra))}."})

    query = IssueQuery(data=request.query_params)
    query.is_valid(raise_exception=True)
    data = query.validated_data

    ordering = data.get("ordering", "row_number")
    if ordering not in {"row_number", "-row_number", "created_at", "-created_at"}:
        raise ValidationError({"ordering": "Unsupported issue ordering."})

    rows = ValidationIssue.objects.filter(ingestion_job=job)
    if data.get("issue_code"):
        rows = rows.filter(error_code=data["issue_code"])

    paginator = AnalyticsPagination()
    paginator.page_size = 50
    page = paginator.paginate_queryset(rows.order_by(ordering, "pk"), request) 
    assert page is not None

    return paginator.get_paginated_response([{"row_number": x.row_number, "field_name": x.field_name, "error_code": x.error_code, "message": x.message, "created_at": x.created_at} for x in page])
