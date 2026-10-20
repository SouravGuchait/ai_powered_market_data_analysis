# config/middleware.py
import logging

logger = logging.getLogger(__name__)


class RequestSourceLogger:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health":
            logger.warning(
                "Unexpected health request: method=%s remote=%s forwarded_for=%s host=%s user_agent=%s",
                request.method,
                request.META.get("REMOTE_ADDR"),
                request.META.get("HTTP_X_FORWARDED_FOR"),
                request.get_host(),
                request.META.get("HTTP_USER_AGENT"),
            )

        return self.get_response(request)