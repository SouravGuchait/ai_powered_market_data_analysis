"""Apply pending database migrations when the application process starts."""

import logging

from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)


def apply_migrations() -> None:
    if not getattr(settings, "AUTO_MIGRATE", True):
        return
    logger.info("database.migrations.start")
    call_command("migrate", interactive=False, verbosity=0)
    logger.info("database.migrations.done")
