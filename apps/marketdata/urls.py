from django.urls import path

from apps.marketdata import views

app_name = "marketdata"

urlpatterns = [
    path("uploads/", views.UploadView.as_view(), name="upload"),
    path("jobs/<uuid:job_id>/", views.JobDetailView.as_view(), name="job-detail"),
]
