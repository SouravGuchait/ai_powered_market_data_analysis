import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User

pytestmark = [pytest.mark.django_db, pytest.mark.integration]

PASSWORD = "CorrectHorseBatteryStaple9"


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def post_json(client, url_name, payload):
    return client.post(
        reverse(url_name),
        payload,
        content_type="application/json",
    )


def test_register_login_refresh_logout_flow_without_csrf():
    client = Client(enforce_csrf_checks=True)

    registered = post_json(
        client,
        "accounts:register",
        {
            "username": "flowuser",
            "email": "FlowUser@Example.COM",
            "last_name": "Flow",
            "password": PASSWORD,
        },
    )

    assert registered.status_code == 201
    assert registered.json()["email"] == "flowuser@example.com"
    assert "password" not in registered.json()
    user = User.objects.get(username="flowuser")
    assert user.check_password(PASSWORD)

    logged_in = post_json(
        client,
        "accounts:login",
        {"username": "flowuser", "password": PASSWORD},
    )

    assert logged_in.status_code == 200
    tokens = logged_in.json()
    assert tokens["user"]["email"] == "flowuser@example.com"
    assert "password" not in tokens["user"]

    access = tokens["access"]
    refresh = tokens["refresh"]

    me = client.get(
        reverse("accounts:me"),
        headers={"authorization": f"Bearer {access}"},
    )
    assert me.status_code == 200
    assert me.json()["username"] == "flowuser"

    refreshed = post_json(
        client,
        "accounts:refresh",
        {"refresh": refresh},
    )

    assert refreshed.status_code == 200
    rotated = refreshed.json()
    assert rotated["refresh"] != refresh

    rotated_me = client.get(
        reverse("accounts:me"),
        headers={"authorization": f"Bearer {rotated['access']}"},
    )
    assert rotated_me.status_code == 200

    reused_old_refresh = post_json(
        client,
        "accounts:refresh",
        {"refresh": refresh},
    )
    assert reused_old_refresh.status_code == 401

    logged_out = post_json(
        client,
        "accounts:logout",
        {"refresh": rotated["refresh"]},
    )
    assert logged_out.status_code == 200
    assert logged_out.json()["detail"] == "You have been successfully logged out"

    reused_rotated_refresh = post_json(
        client,
        "accounts:refresh",
        {"refresh": rotated["refresh"]},
    )
    assert reused_rotated_refresh.status_code == 401

    logged_in_by_email = post_json(
        client,
        "accounts:login",
        {"username": "flowuser@example.com", "password": PASSWORD},
    )
    assert logged_in_by_email.status_code == 200
    assert logged_in_by_email.json()["user"]["username"] == "flowuser"
