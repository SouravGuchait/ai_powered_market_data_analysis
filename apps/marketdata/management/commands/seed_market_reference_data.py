"""Seed the exchange and instrument reference rows used by synthetic data."""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.marketdata.models import Exchange, Instrument
from scripts.generate_market_data import EXCHANGES, SYMBOL_POOL, build_instruments

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Create or update the reference exchanges and instruments for fixture data."

    def handle(self, *args, **options):
        exchange_rows = build_instruments(len(SYMBOL_POOL))
        exchanges_by_code = {}
        exchange_created = 0
        exchange_updated = 0
        instrument_created = 0
        instrument_updated = 0

        with transaction.atomic():
            for code, name, timezone in EXCHANGES:
                exchange, created = Exchange.objects.update_or_create(
                    code=code,
                    defaults={"name": name, "timezone": timezone},
                )
                exchanges_by_code[code] = exchange
                if created:
                    exchange_created += 1
                else:
                    exchange_updated += 1

            for row in exchange_rows:
                _, created = Instrument.objects.update_or_create(
                    exchange=exchanges_by_code[row["exchange"]],
                    symbol=row["symbol"],
                    defaults={
                        "name": row["name"],
                        "asset_class": row["asset_class"],
                        "currency": row["currency"],
                        "is_active": True,
                    },
                )
                if created:
                    instrument_created += 1
                else:
                    instrument_updated += 1

        logger.info(
            "marketdata.reference_data.seeded exchanges_created=%d exchanges_updated=%d instruments_created=%d instruments_updated=%d",
            exchange_created,
            exchange_updated,
            instrument_created,
            instrument_updated,
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Reference data ready: "
                f"exchanges created={exchange_created}, updated={exchange_updated}; "
                f"instruments created={instrument_created}, updated={instrument_updated}."
            )
        )
