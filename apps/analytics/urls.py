from django.urls import path

from apps.analytics import views

app_name = "analytics"

urlpatterns = [
    path("daily-returns/", views.DailyReturnsView.as_view(), name="daily-returns"),
    path("moving-averages/", views.MovingAveragesView.as_view(), name="moving-averages"),
    path("rolling-volatility/", views.RollingVolatilityView.as_view(), name="rolling-volatility"),
    path("drawdown/", views.DrawdownView.as_view(), name="drawdown"),
    path("summary/", views.AnalyticsSummaryView.as_view(), name="summary"),
]
