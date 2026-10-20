import re

import django.contrib.auth.validators
from django.db import migrations, models

USERNAME_FIELD = models.CharField(
    error_messages={"unique": "A user with that username already exists."},
    help_text=(
        "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."
    ),
    max_length=150,
    unique=True,
    validators=[django.contrib.auth.validators.UnicodeUsernameValidator()],
    verbose_name="username",
)


def populate_usernames(apps, schema_editor):
    user_model = apps.get_model("accounts", "User")
    used = set()
    for user in user_model.objects.order_by("pk"):
        base = (user.email or "user").split("@")[0].lower()
        base = re.sub(r"[^a-z0-9._+-]", "", base).strip("._+-")[:140] or "user"
        candidate = base
        suffix = 1
        while (
            candidate in used
            or user_model.objects.filter(username=candidate).exclude(pk=user.pk).exists()
        ):
            suffix += 1
            candidate = f"{base}{suffix}"[:150]
        user.username = candidate
        fields = ["username"]
        normalized_email = (user.email or "").strip().lower()
        if normalized_email and normalized_email != user.email:
            clash = (
                user_model.objects.filter(email__iexact=normalized_email)
                .exclude(pk=user.pk)
                .exists()
            )
            if not clash:
                user.email = normalized_email
                fields.append("email")
        user.save(update_fields=fields)
        used.add(candidate)


class Migration(migrations.Migration):

    dependencies = [  # noqa: RUF012
        ("accounts", "0001_initial"),
    ]

    operations = [  # noqa: RUF012
        migrations.AddField(
            model_name="user",
            name="username",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Required. 150 characters or fewer. Letters, digits and "
                    "@/./+/-/_ only."
                ),
                max_length=150,
                null=True,
                validators=[django.contrib.auth.validators.UnicodeUsernameValidator()],
                verbose_name="username",
            ),
        ),
        migrations.RunPython(populate_usernames, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="username",
            field=USERNAME_FIELD,
        ),
        migrations.AlterField(
            model_name="user",
            name="last_name",
            field=models.CharField(db_column="lastname", max_length=150),
        ),
    ]
