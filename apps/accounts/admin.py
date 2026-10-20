from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User


class UserCreationAdminForm(UserCreationForm):  # pyright: ignore[reportMissingTypeArgument]
    class Meta:
        model = User
        fields = ("username", "email", "last_name")


class UserChangeAdminForm(UserChangeForm):  # pyright: ignore[reportMissingTypeArgument]
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):  # pyright: ignore[reportMissingTypeArgument]
    form = UserChangeAdminForm
    add_form = UserCreationAdminForm

    ordering = ("username",)
    list_display = ("username", "email", "last_name", "is_staff", "is_active")
    search_fields = ("username", "email", "last_name")
    fieldsets = (
        (None, {"fields": ("username", "email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "last_name",
                    "password1",
                    "password2",
                ),
            },
        ),
    )
