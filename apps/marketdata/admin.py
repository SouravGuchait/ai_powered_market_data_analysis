"""Admin inspection for market-data models.

Large tables use raw-id inputs instead of select boxes, and internal storage
references (storage keys, raw row payloads) stay out of the change forms.
"""

from django.contrib import admin

from apps.marketdata.models import (
    AuditEvent,
    Exchange,
    IngestionFile,
    IngestionJob,
    Instrument,
    MetricSnapshot,
    PriceRecord,
    ValidationIssue,
)


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "timezone")
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "exchange", "name", "asset_class", "currency", "is_active")
    list_filter = ("exchange", "asset_class", "is_active")
    search_fields = ("symbol", "name")
    raw_id_fields = ("exchange",)


@admin.register(PriceRecord)
class PriceRecordAdmin(admin.ModelAdmin):
    list_display = (
        "instrument",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "source",
    )
    list_filter = ("instrument__exchange", "source")
    date_hierarchy = "trading_date"
    search_fields = ("instrument__symbol",)
    raw_id_fields = ("instrument", "ingestion_job")
    list_per_page = 50


@admin.register(IngestionJob)
class IngestionJobAdmin(admin.ModelAdmin):
    list_display = (
        "job_id",
        "created_by",
        "source_type",
        "status",
        "requested_at",
        "completed_at",
        "rows_received",
        "rows_accepted",
        "rows_rejected",
        "error_code",
    )
    list_filter = ("status", "source_type")
    date_hierarchy = "requested_at"
    search_fields = ("task_id", "error_code")
    raw_id_fields = ("created_by",)
    readonly_fields = ("id", "requested_at", "created_at", "updated_at")
    list_per_page = 50

    @admin.display(description="Job")
    def job_id(self, obj: IngestionJob) -> str:
        return str(obj.id)


@admin.register(IngestionFile)
class IngestionFileAdmin(admin.ModelAdmin):
    # storage_key is an internal storage reference; keep it out of both the
    # list and the change form.
    list_display = ("original_filename", "content_type", "file_size_bytes", "created_at")
    search_fields = ("original_filename", "checksum")
    raw_id_fields = ("ingestion_job",)
    exclude = ("storage_key",)


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    # raw_row may contain user data; show rule, position, and message only.
    list_display = ("ingestion_job", "row_number", "field_name", "error_code", "message")
    list_filter = ("error_code",)
    search_fields = ("message", "field_name", "error_code")
    raw_id_fields = ("ingestion_job",)
    exclude = ("raw_row",)
    list_per_page = 50


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "user",
        "action",
        "object_type",
        "object_id",
        "request_id",
        "ip_address",
    )
    list_filter = ("action", "object_type")
    search_fields = ("action", "object_type", "object_id", "request_id")
    raw_id_fields = ("user",)
    date_hierarchy = "created_at"
    exclude = ("metadata",)


@admin.register(MetricSnapshot)
class MetricSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "instrument",
        "as_of_date",
        "metric_type",
        "lookback_days",
        "value",
        "calculation_version",
    )
    list_filter = ("metric_type", "calculation_version")
    date_hierarchy = "as_of_date"
    search_fields = ("instrument__symbol",)
    raw_id_fields = ("instrument",)
    list_per_page = 50
