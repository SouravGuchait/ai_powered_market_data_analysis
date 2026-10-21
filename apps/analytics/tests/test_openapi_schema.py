import yaml
from django.core.management import call_command
from drf_spectacular.drainage import reset_generator_stats


def test_openapi_schema_generates_and_validates(tmp_path):
    schema_path = tmp_path / "openapi.yaml"
    reset_generator_stats()
    call_command("spectacular", file=schema_path, validate=True, fail_on_warn=True)

    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    paths = schema["paths"]
    assert {
        "/api/v1/auth/register/",
        "/api/v1/auth/login/",
        "/api/v1/auth/token/refresh/",
        "/api/v1/auth/logout/",
        "/api/v1/auth/me/",
        "/api/v1/uploads/",
        "/api/v1/jobs/{job_id}/",
        "/api/v1/jobs/{job_id}/issues/",
        "/api/v1/analytics/daily-returns/",
        "/api/v1/analytics/moving-averages/",
        "/api/v1/analytics/rolling-volatility/",
        "/api/v1/analytics/drawdown/",
        "/api/v1/analytics/summary/",
    } <= paths.keys()

    assert {"201", "400", "429"} <= paths["/api/v1/auth/register/"]["post"]["responses"].keys()
    assert {"202", "400", "401", "413", "415", "500", "503"} <= paths[
        "/api/v1/uploads/"
    ]["post"]["responses"].keys()
    assert "multipart/form-data" in paths["/api/v1/uploads/"]["post"]["requestBody"][
        "content"
    ]

    moving_average = paths["/api/v1/analytics/moving-averages/"]["get"]
    assert "window" in {parameter["name"] for parameter in moving_average["parameters"]}
    assert "401" in moving_average["responses"]
