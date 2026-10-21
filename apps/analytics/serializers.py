from rest_framework import serializers


class AnalyticsPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    value = serializers.FloatField(allow_null=True)


class AnalyticsSeriesSerializer(serializers.Serializer):
    results = AnalyticsPointSerializer(many=True)
    symbol = serializers.CharField()
    exchange = serializers.CharField()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    metric = serializers.ChoiceField(choices=("daily_return", "moving_average", "rolling_volatility", "max_drawdown"))
    units = serializers.ChoiceField(choices=("fraction", "price", "annualized_fraction"))
    window = serializers.IntegerField(required=False)
    annualized = serializers.BooleanField(required=False)
    annualization_days = serializers.IntegerField(required=False)
    return_ddof = serializers.IntegerField(required=False)
    running_peak_policy = serializers.CharField(required=False)


class PriceAggregateSerializer(serializers.Serializer):
    observation_count = serializers.IntegerField()
    minimum_close = serializers.DecimalField(max_digits=18, decimal_places=6, allow_null=True)
    maximum_close = serializers.DecimalField(max_digits=18, decimal_places=6, allow_null=True)
    average_close = serializers.DecimalField(max_digits=18, decimal_places=6, allow_null=True)


class AnalyticsSummarySerializer(serializers.Serializer):
    symbol = serializers.CharField(allow_null=True)
    exchange = serializers.CharField(allow_null=True)
    start_date = serializers.DateField(allow_null=True)
    end_date = serializers.DateField(allow_null=True)
    price_aggregate = PriceAggregateSerializer()


class ValidationIssueSerializer(serializers.Serializer):
    row_number = serializers.IntegerField(allow_null=True)
    field_name = serializers.CharField(allow_null=True)
    error_code = serializers.CharField()
    message = serializers.CharField()
    created_at = serializers.DateTimeField()


class ValidationIssuePageSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = ValidationIssueSerializer(many=True)
