import logging
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle, ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.accounts.models import User
from apps.accounts.serializers import (
    LoginSerializer,
    RegisterSerializer,
    UserSerializer,
)

AUTH_ERROR = OpenApiResponse(
    response=OpenApiTypes.OBJECT,
    description="Authentication, validation, or throttling error. DRF returns `detail` for authentication/throttle failures and field-keyed errors for validation; credentials are never included.",
)

logger = logging.getLogger(__name__)


class RegisterView(APIView):
    authentication_classes: list[type[object]] = []  # noqa: RUF012
    permission_classes: list[type[BasePermission]] = [AllowAny]  # noqa: RUF012
    throttle_classes: list[type[BaseThrottle]] = [  # noqa: RUF012
        ScopedRateThrottle
    ]
    throttle_scope = "register"

    @extend_schema(request=RegisterSerializer, responses={201: UserSerializer, 400: AUTH_ERROR, 429: AUTH_ERROR}, description="Creates an account. Registration is rate limited.")
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = cast(User, serializer.save())
        logger.info("auth.registration.succeeded user_id=%s", user.pk)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    throttle_classes: list[type[BaseThrottle]] = [ScopedRateThrottle]  # noqa: RUF012
    throttle_scope = "login"
    serializer_class = LoginSerializer

    @extend_schema(responses={200: LoginSerializer, 400: AUTH_ERROR, 401: AUTH_ERROR, 429: AUTH_ERROR}, description="Exchanges username and password for JWT access and refresh tokens. Rate limited.")

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            logger.info("auth.login.succeeded")
        else:
            logger.warning("auth.login.failed status=%s", response.status_code)
        return response


class RefreshView(TokenRefreshView):
    throttle_classes: list[type[BaseThrottle]] = [ScopedRateThrottle]  # noqa: RUF012
    throttle_scope = "refresh"

    @extend_schema(responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Rotated access and refresh tokens."), 400: AUTH_ERROR, 401: AUTH_ERROR, 429: AUTH_ERROR}, description="Refreshes the access token and rotates the refresh token. Rate limited.")
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class LogoutView(TokenBlacklistView):
    throttle_classes: list[type[BaseThrottle]] = [ScopedRateThrottle]  # noqa: RUF012
    throttle_scope = "logout"

    @extend_schema(responses={200: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Refresh token blacklisted."), 400: AUTH_ERROR, 401: AUTH_ERROR, 429: AUTH_ERROR}, description="Blacklists the submitted refresh token. Rate limited.")

    def post(self, request, *args, **kwargs):
        try:
            response = super().post(request, *args, **kwargs)
        except Exception:
            logger.exception("auth.logout.failed")
            raise
        if response.status_code != status.HTTP_200_OK:
            return response
        logger.info("auth.logout.succeeded")
        return Response(
            {"detail": "You have been successfully logged out"},
            status=status.HTTP_200_OK,
        )


class MeView(APIView):
    @extend_schema(responses={200: UserSerializer, 401: AUTH_ERROR}, description="Returns the authenticated user's public profile.")
    def get(self, request):
        return Response(UserSerializer(request.user).data)
