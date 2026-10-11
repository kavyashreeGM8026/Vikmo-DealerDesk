import csv
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Product


class Command(BaseCommand):
    help = "Load the files in data/ into the database. Must be safe to run more than once."

    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR  # <repo root>/data
        catalogue_path = data_dir / "catalogue.csv"
        if not catalogue_path.exists():
            raise CommandError(f"Catalogue file not found: {catalogue_path}")

        created = 0
        skipped = 0
        with catalogue_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))

        with transaction.atomic():
            for row in rows:
                sku = (row.get("sku") or "").strip()
                if not sku:
                    continue
                stock = int((row.get("stock") or "0").strip() or 0)
                defaults = {
                    "name": (row.get("name") or "").strip(),
                    "category": (row.get("category") or "").strip(),
                    "brand": (row.get("brand") or "").strip(),
                    "vehicle_fitment": (row.get("vehicle_fitment") or "").strip(),
                    "price": Decimal((row.get("price") or "0").strip()),
                    "description": (row.get("description") or "").strip(),
                    "stock": stock,
                }
                _, was_created = Product.objects.get_or_create(sku=sku, defaults=defaults)
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Catalogue: {created} created, {skipped} already present "
                f"(total rows read: {len(rows)})."
            )
        )
