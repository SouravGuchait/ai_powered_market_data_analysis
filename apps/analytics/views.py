from typing import final

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.api import _series, summary
from apps.analytics.serializers import (
    AnalyticsSeriesSerializer,
    AnalyticsSummarySerializer,
)

ANALYTICS_PARAMETERS = [
    OpenApiParameter("symbol", str, required=True, description="Instrument symbol; case-insensitive."),
    OpenApiParameter("exchange", str, description="Exchange code; required when the symbol is ambiguous."),
    OpenApiParameter("start_date", str, required=True, description="Inclusive ISO date, YYYY-MM-DD."),
    OpenApiParameter("end_date", str, required=True, description="Inclusive ISO date, YYYY-MM-DD; range may not exceed 3,660 days."),
    OpenApiParameter("page", int, description="One-based result page; default 1."),
    OpenApiParameter("page_size", int, description="Results per page; default 50, maximum 100."),
    OpenApiParameter("ordering", str, enum=["trading_date", "-trading_date"], description="Date order; default ascending."),
]
def series_example(metric, units, value):
    return OpenApiExample(
        f"Synthetic {metric} series",
        value={"results": [{"date": "2024-01-03", "value": value}], "symbol": "SYN1", "exchange": "XNAS", "start_date": "2024-01-01", "end_date": "2024-01-31", "metric": metric, "units": units},
        response_only=True,
    )


class AnalyticsSeriesView(APIView):
    permission_classes = [IsAuthenticated]  # noqa: RUF012
    metric = ""

    @extend_schema(
        parameters=ANALYTICS_PARAMETERS,
        responses={200: AnalyticsSeriesSerializer, 400: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Invalid or inaccessible filter; DRF field-error object."), 401: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Authentication required.")},
        examples=[series_example("daily_return", "fraction", 0.01)],
        description=("Returns owner-scoped calculated observations in pages of 50 by default (maximum 100). "
                     "Metric values are fractions unless `units` is `price`; multiply fractional returns, "
                     "volatility, and drawdown by 100 only for percentage display. Daily return uses the "
                     "latest earlier close as calculation context. Moving average uses the selected 5- or "
                     "20-observation close window and includes warm-up history before the requested start, "
                     "but omits that context from results. Volatility uses a 5- or 20-return window (n+1 "
                     "closes), sample standard deviation (ddof=1), annualized by sqrt(252). Drawdown resets "
                     "its running peak at the requested period boundary."),
    )
    def get(self, request):
        return _series(request, self.metric)


class DailyReturnsView(AnalyticsSeriesView):
    metric = "daily_return"


class MovingAveragesView(AnalyticsSeriesView):
    metric = "moving_average"

    @extend_schema(parameters=ANALYTICS_PARAMETERS + [OpenApiParameter("window", int, enum=[5, 20], description="Close observations in the moving-average window; default 5.")], responses={200: AnalyticsSeriesSerializer, 400: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Invalid or inaccessible filter; DRF field-error object."), 401: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Authentication required.")}, description="Calculated simple close-price moving average. The selected window uses 5 or 20 closes; earlier warm-up closes may be used, but only requested dates are returned.", examples=[series_example("moving_average", "price", 10.5)])
    def get(self, request):
        return super().get(request)


class RollingVolatilityView(AnalyticsSeriesView):
    metric = "rolling_volatility"

    @extend_schema(parameters=ANALYTICS_PARAMETERS + [OpenApiParameter("window", int, enum=[5, 20], description="Daily returns in the volatility window; default 20, requiring n+1 closes.")], responses={200: AnalyticsSeriesSerializer, 400: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Invalid or inaccessible filter; DRF field-error object."), 401: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Authentication required.")}, description="Annualized rolling sample standard deviation of daily fractional returns (ddof=1, annualization factor 252). An n-return window requires n+1 closes; earlier warm-up observations are omitted from results.", examples=[series_example("rolling_volatility", "annualized_fraction", 0.2)])
    def get(self, request):
        return super().get(request)


class DrawdownView(AnalyticsSeriesView):
    metric = "drawdown"

    @extend_schema(
        parameters=ANALYTICS_PARAMETERS,
        responses={
            200: AnalyticsSeriesSerializer,
            400: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Invalid or inaccessible filter; DRF field-error object."),
            401: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Authentication required."),
        },
        description="Fractional peak-to-trough drawdown; the running peak is reset at the requested period boundary.",
        examples=[series_example("max_drawdown", "fraction", -0.1)],
    )
    def get(self, request):
        return super().get(request)

@final
class AnalyticsSummaryView(APIView):
    
    permission_classes = [IsAuthenticated]  # noqa: RUF012

    @extend_schema(
        parameters=ANALYTICS_PARAMETERS,
        responses={200: AnalyticsSummarySerializer, 400: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Invalid or inaccessible filter; DRF field-error object."), 401: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Authentication required.")},
        description="Returns database aggregates of raw close prices for the inclusive requested dates. Prices are not percentages or derived metrics.",
    )
    def get(self, request):
        return Response(summary(request))
