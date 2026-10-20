import pytest
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts import views
from apps.accounts.models import User

pytestmark = pytest.mark.django_db

VALID_PASSWORD = "CorrectHorseBatteryStaple9"


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def low_throttle_rates(monkeypatch):
    monkeypatch.setattr(
        ScopedRateThrottle,
        "THROTTLE_RATES",
        {
            "register": "2/min",
            "login": "2/min",
            "refresh": "2/min",
            "logout": "2/min",
        },
    )


def register(
    client,
    username="newuser",
    email="newuser@example.com",
    last_name="Doe",
    password=VALID_PASSWORD,
):
    return client.post(
        reverse("accounts:register"),
        {
            "username": username,
            "email": email,
            "last_name": last_name,
            "password": password,
        },
        content_type="application/json",
    )


def login(client, identifier, password=VALID_PASSWORD):
    return client.post(
        reverse("accounts:login"),
        {"username": identifier, "password": password},
        content_type="application/json",
    )


def bearer(access):
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def make_user(**overrides):
    values = {
        "username": "owner",
        "email": "owner@example.com",
        "last_name": "Owner",
        "password": VALID_PASSWORD,
    }
    values.update(overrides)
    return User.objects.create_user(**values)


def test_registration_creates_user_with_hashed_password(client):
    response = register(client, username="NewUser", email="NewUser@Example.COM")

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "NewUser"
    assert body["email"] == "newuser@example.com"
    assert body["last_name"] == "Doe"
    assert "password" not in body
    user = User.objects.get(username="NewUser")
    assert user.password != VALID_PASSWORD
    assert user.check_password(VALID_PASSWORD)
    assert user.email == "newuser@example.com"


def test_registration_requires_username_email_and_last_name(client):
    assert register(client, username="").status_code == 400
    assert register(client, username="noemail", email="").status_code == 400
    assert register(client, username="nolast", last_name="").status_code == 400


def test_registration_rejects_duplicate_email_case_insensitively(client):
    assert register(client, username="first", email="Owner@Example.COM").status_code == 201

    response = register(client, username="second", email="owner@example.com")

    assert response.status_code == 400
    assert "email" in response.json()
    assert User.objects.filter(email="owner@example.com").count() == 1


def test_registration_rejects_duplicate_username_case_insensitively(client):
    assert register(client, username="Owner", email="first@example.com").status_code == 201

    response = register(client, username="owner", email="second@example.com")

    assert response.status_code == 400
    assert "username" in response.json()
    assert User.objects.filter(username__iexact="owner").count() == 1


def test_database_rejects_duplicate_normalized_email():
    make_user()

    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(username="other", email="OWNER@EXAMPLE.COM")


def test_database_rejects_duplicate_username():
    make_user()

    with pytest.raises(IntegrityError), transaction.atomic():
        make_user(email="other@example.com")


@pytest.mark.parametrize("password", ["short", "12345678901", "password123"])
def test_registration_rejects_invalid_passwords(client, password):
    response = register(client, username="weak", email="weak@example.com", password=password)

    assert response.status_code == 400
    assert "password" in response.json()
    assert not User.objects.filter(username="weak").exists()


def test_registration_rejects_password_similar_to_identity(client):
    response = register(
        client,
        username="similar",
        email="similar@example.com",
        password="similar@example.com",
    )

    assert response.status_code == 400
    assert "password" in response.json()


def test_login_with_username_returns_tokens_and_user(client):
    make_user()

    response = login(client, "Owner")

    assert response.status_code == 200
    body = response.json()
    assert body["access"]
    assert body["refresh"]
    assert body["user"]["username"] == "owner"
    assert body["user"]["email"] == "owner@example.com"


def test_login_with_email_returns_tokens(client):
    make_user()

    response = login(client, "Owner@Example.COM")

    assert response.status_code == 200
    assert response.json()["user"]["username"] == "owner"


def test_invalid_credentials_do_not_reveal_account_existence(client):
    make_user()

    wrong_password = login(client, "owner", "WrongPassword123")
    unknown_identifier = login(client, "missing@example.com", "WrongPassword123")

    assert wrong_password.status_code == 401
    assert unknown_identifier.status_code == 401
    assert wrong_password.json() == unknown_identifier.json()
    assert "owner" not in wrong_password.content.decode()


def test_inactive_user_cannot_authenticate(client):
    make_user(is_active=False)

    response = login(client, "owner")

    assert response.status_code == 401
    assert "access" not in response.json()


def test_me_requires_access_token(client):
    response = client.get(reverse("accounts:me"))

    assert response.status_code == 401


def test_me_returns_user_with_valid_access_token(client):
    make_user()
    access = login(client, "owner").json()["access"]

    response = client.get(reverse("accounts:me"), **bearer(access))

    assert response.status_code == 200
    assert response.json()["username"] == "owner"


def test_refresh_returns_new_access_token(client):
    make_user()
    refresh = login(client, "owner").json()["refresh"]

    response = client.post(
        reverse("accounts:refresh"),
        {"refresh": refresh},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["access"]


def test_logout_blacklists_the_refresh_token(client):
    make_user()
    refresh = login(client, "owner").json()["refresh"]

    logout = client.post(
        reverse("accounts:logout"),
        {"refresh": refresh},
        content_type="application/json",
    )
    assert logout.status_code == 200

    reused = client.post(
        reverse("accounts:refresh"),
        {"refresh": refresh},
        content_type="application/json",
    )
    assert reused.status_code == 401


def test_registration_and_login_do_not_require_csrf():
    client = Client(enforce_csrf_checks=True)

    created = register(client, username="noscript", email="noscript@example.com")

    assert created.status_code == 201
    assert login(client, "noscript").status_code == 200


def test_auth_routes_use_api_auth_prefix():
    assert reverse("accounts:register") == "/api/v1/auth/register/"
    assert reverse("accounts:login") == "/api/v1/auth/login/"
    assert reverse("accounts:refresh") == "/api/v1/auth/token/refresh/"
    assert reverse("accounts:logout") == "/api/v1/auth/logout/"
    assert reverse("accounts:me") == "/api/v1/auth/me/"


def test_credential_endpoints_declare_throttle_scopes():
    from django.conf import settings

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    scopes = {
        views.RegisterView.throttle_scope,
        views.LoginView.throttle_scope,
        views.RefreshView.throttle_scope,
    }

    assert scopes == {"register", "login", "refresh"}
    for scope in scopes:
        assert rates[scope]
    for view in (views.RegisterView, views.LoginView, views.RefreshView):
        assert view.throttle_classes == [ScopedRateThrottle]


def test_repeated_invalid_logins_are_throttled(client, low_throttle_rates):
    for _ in range(2):
        assert login(client, "nobody", "WrongPassword123").status_code == 401

    response = login(client, "nobody", "WrongPassword123")

    assert response.status_code == 429
    body = response.content.decode()
    assert "nobody" not in body
    assert "WrongPassword123" not in body


def test_repeated_registrations_are_throttled(client, low_throttle_rates):
    assert register(client, username="first", email="first@example.com").status_code == 201
    assert register(client, username="second", email="second@example.com").status_code == 201

    response = register(client, username="third", email="third@example.com")

    assert response.status_code == 429
    assert "third@example.com" not in response.content.decode()
    assert not User.objects.filter(username="third").exists()


def test_create_superuser_requires_and_normalizes_identity():
    user = User.objects.create_superuser(
        username="Admin",
        email="Admin@Example.COM",
        last_name="Root",
        password=VALID_PASSWORD,
    )

    assert user.username == "Admin"
    assert user.email == "admin@example.com"
    assert user.last_name == "Root"
    assert user.is_staff is True
    assert user.is_superuser is True


def test_admin_add_form_creates_user():
    from apps.accounts.admin import UserCreationAdminForm

    form = UserCreationAdminForm(
        data={
            "username": "Admin",
            "email": "Admin@Example.COM",
            "last_name": "Root",
            "password1": VALID_PASSWORD,
            "password2": VALID_PASSWORD,
        }
    )

    assert form.is_valid(), form.errors
    user = form.save()
    assert user.username == "Admin"
    assert user.email == "admin@example.com"
    assert user.check_password(VALID_PASSWORD)
