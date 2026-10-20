from typing import Any, ClassVar

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager["User"]):
    @classmethod
    def normalize_email(cls, email: str | None) -> str:
        """Lowercase the whole address so identity is case-insensitive."""
        return super().normalize_email(email).lower()

    def _create_user(
        self,
        username: str,
        email: str,
        password: str | None,
        **extra_fields: Any,
    ) -> "User":
        if not username:
            raise ValueError("The username must be set")
        if not email:
            raise ValueError("The email address must be set")
        user = self.model(
            username=username,
            email=self.normalize_email(email),
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self,
        username: str,
        email: str,
        password: str | None = None,
        **extra_fields: Any,
    ) -> "User":
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(username, email, password, **extra_fields)

    def create_superuser(
        self,
        username: str,
        email: str,
        password: str | None = None,
        **extra_fields: Any,
    ) -> "User":
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(username, email, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField("email address", unique=True)
    last_name = models.CharField(max_length=150, db_column="lastname")

    objects: ClassVar[Any] = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["email", "last_name"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.__class__.objects.normalize_email(self.email)
        super().save(*args, **kwargs)
