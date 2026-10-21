"""Disposable cache for owner-scoped analytics responses."""

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from rest_framework.response import Response

logger = logging.getLogger(__name__)
CACHE_VERSION = "analytics-v1"


def _generation_key(user_id: int) -> str:
    return f"{CACHE_VERSION}:owner:{user_id}:generation"


def analytics_response(
    user_id: int,
    endpoint: str,
    filters: dict[str, Any],
    build: Callable[[], Any],
    *,
    as_response: bool = True,
) -> Any:
    """Cache only successful serialized responses; cache failure is a miss."""
    try:
        generation = cache.get(_generation_key(user_id), 0)
        identity = json.dumps(
            [CACHE_VERSION, user_id, generation, endpoint, filters],
            sort_keys=True,
            separators=(",", ":"),
            cls=DjangoJSONEncoder,
        )
        key = f"{CACHE_VERSION}:{hashlib.sha256(identity.encode()).hexdigest()}"
        payload = cache.get(key)
        if payload is not None:
            logger.debug("analytics.cache.hit endpoint=%s user_id=%s", endpoint, user_id)
            data = json.loads(payload)
            return Response(data) if as_response else data
    except Exception:  # noqa: BLE001 - cache is optional and must not fail the request
        logger.warning("analytics.cache.read_failed endpoint=%s user_id=%s", endpoint, user_id)
        key = None

    response = build()
    if getattr(response, "status_code", 200) < 400 and key is not None:
        try:
            cache.set(
                key,
                json.dumps(
                    response.data if isinstance(response, Response) else response,
                    cls=DjangoJSONEncoder,
                    separators=(",", ":"),
                ),
                timeout=settings.ANALYTICS_CACHE_TTL,
            )
            logger.debug("analytics.cache.miss endpoint=%s user_id=%s", endpoint, user_id)
        except Exception:  # noqa: BLE001 - cache is optional and must not fail the request
            logger.warning("analytics.cache.write_failed endpoint=%s user_id=%s", endpoint, user_id)
    return response


def invalidate_owner_analytics(user_id: int | None) -> None:
    """Advance this owner's cache namespace after durable ingestion success."""
    if user_id is None:
        return
    key = _generation_key(user_id)
    try:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=None)
    except Exception:  # noqa: BLE001 - invalidation failure is bounded by TTL
        logger.warning("analytics.cache.invalidate_failed user_id=%s", user_id)
