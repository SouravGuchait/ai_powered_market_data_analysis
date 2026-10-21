from typing import final

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.api import _series, summary
from apps.analytics.serializers import (
    AnalyticsSeriesSerializer,
    AnalyticsSummarySerializer,
)


class AnalyticsSeriesView(APIView):
    permission_classes = [IsAuthenticated]  # noqa: RUF012
    metric = ""

    @extend_schema(responses=AnalyticsSeriesSerializer)
    def get(self, request):
        return _series(request, self.metric)


class DailyReturnsView(AnalyticsSeriesView):
    metric = "daily_return"


class MovingAveragesView(AnalyticsSeriesView):
    metric = "moving_average"


class RollingVolatilityView(AnalyticsSeriesView):
    metric = "rolling_volatility"


class DrawdownView(AnalyticsSeriesView):
    metric = "drawdown"

@final
class AnalyticsSummaryView(APIView):
    
    permission_classes = [IsAuthenticated]  # noqa: RUF012

    @extend_schema(responses=AnalyticsSummarySerializer)
    def get(self, request):
        return Response(summary(request))
