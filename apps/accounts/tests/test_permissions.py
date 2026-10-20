from types import SimpleNamespace

import pytest

from apps.accounts.models import User
from apps.accounts.permissions import IsOwner

pytestmark = pytest.mark.django_db


def test_user_cannot_access_another_users_job(rf):
    owner = User.objects.create_user(
        username="owner",
        email="owner@example.com",
        last_name="Owner",
        password="CorrectHorseBatteryStaple9",
    )
    other = User.objects.create_user(
        username="other",
        email="other@example.com",
        last_name="Other",
        password="CorrectHorseBatteryStaple9",
    )
    request = rf.get("/")
    request.user = owner
    permission = IsOwner()

    job = SimpleNamespace(owner_id=other.pk)

    assert permission.has_object_permission(request, None, job) is False
    job.owner_id = owner.pk
    assert permission.has_object_permission(request, None, job) is True
    assert permission.has_object_permission(request, None, SimpleNamespace()) is False
