from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    """Authenticate with an exact username or an email address."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        if username is None:
            username = kwargs.get(getattr(user_model, "USERNAME_FIELD", "username"))
        if username is None or password is None:
            return None

        user = user_model.objects.filter(username__iexact=username).first()
        if user is None:
            user = user_model.objects.filter(email__iexact=username).first()
        if user is None:
            user_model().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
