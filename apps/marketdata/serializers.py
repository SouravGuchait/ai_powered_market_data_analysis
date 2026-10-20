from rest_framework import serializers

from apps.marketdata.models import IngestionJob


class IngestionJobSerializer(serializers.ModelSerializer):
    """Owner-facing job status; internal storage and task state stay private."""

    class Meta:
        model = IngestionJob
        fields = (
            "id",
            "status",
            "source_type",
            "rows_received",
            "rows_accepted",
            "rows_rejected",
            "error_code",
            "error_summary",
            "requested_at",
            "started_at",
            "completed_at",
        )
        read_only_fields = fields


class UploadAcceptedSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    replayed = serializers.BooleanField()


class UploadRequestSerializer(serializers.Serializer):
    file = serializers.FileField()
    idempotency_key = serializers.CharField(required=False, max_length=64)
