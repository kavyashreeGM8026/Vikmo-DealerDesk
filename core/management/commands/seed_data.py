import csv
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from core.models import Dealer, Product

DEALER_COLUMNS = ["dealer_code", "name", "city", "phone", "email", "gstin", "credit_limit"]
MAX_CREDIT_LIMIT = Decimal(10) ** 10  # DecimalField(max_digits=12, decimal_places=2)


class Command(BaseCommand):
    help = "Load the files in data/ into the database. Must be safe to run more than once."

    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR  # <repo root>/data
        catalogue_path = data_dir / "catalogue.csv"
        dealers_path = data_dir / "dealers.csv"
        # Check every file first so a missing file never leaves a half-seeded database.
        if not catalogue_path.exists():
            raise CommandError(f"Catalogue file not found: {catalogue_path}")
        if not dealers_path.exists():
            raise CommandError(f"Dealers file not found: {dealers_path}")

        self._seed_catalogue(catalogue_path)
        self._seed_dealers(dealers_path)

    # ---- catalogue.csv (unchanged behaviour) -------------------------------------------
    def _seed_catalogue(self, catalogue_path):
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

    # ---- dealers.csv ---------------------------------------------------------------------
    def _seed_dealers(self, dealers_path):
        # utf-8-sig also copes with a BOM at the start of the file (common from Excel).
        with dealers_path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            reader.fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
            missing = [col for col in DEALER_COLUMNS if col not in reader.fieldnames]
            if missing:
                raise CommandError(
                    f"{dealers_path.name} is missing required column(s): {', '.join(missing)}"
                )
            rows = [(reader.line_num, row) for row in reader]

        created = 0
        skipped = 0
        invalid = 0
        with transaction.atomic():
            for line_no, row in rows:
                if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
                    continue  # completely blank line
                values, problem = self._parse_dealer_row(row)
                if problem:
                    invalid += 1
                    self.stderr.write(
                        self.style.WARNING(f"dealers.csv line {line_no} skipped: {problem}")
                    )
                    continue
                # Existing dealers are left exactly as they are (same rule as products).
                _, was_created = Dealer.objects.get_or_create(
                    dealer_code=values.pop("dealer_code"), defaults=values
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Dealers: {created} created, {skipped} already present, "
                f"{invalid} invalid rows skipped (total rows read: {len(rows)})."
            )
        )

    @staticmethod
    def _parse_dealer_row(row):
        """Return (values, None) for a good row, or (None, "reason") for a bad one."""
        values = {col: (row.get(col) or "").strip() for col in DEALER_COLUMNS}

        if not values["dealer_code"]:
            return None, "dealer_code is empty"
        if not values["name"]:
            return None, f"name is empty for dealer {values['dealer_code']}"

        for field_name in ("dealer_code", "name", "city", "phone", "email", "gstin"):
            max_length = Dealer._meta.get_field(field_name).max_length
            if len(values[field_name]) > max_length:
                return None, f"{field_name} is longer than {max_length} characters"

        if values["email"]:
            try:
                validate_email(values["email"])
            except DjangoValidationError:
                return None, f"invalid email '{values['email']}'"

        raw_limit = values["credit_limit"]
        if raw_limit == "":
            values["credit_limit"] = Decimal("0.00")
        else:
            try:
                limit = Decimal(raw_limit)
                if not limit.is_finite():
                    raise InvalidOperation
                exact = limit.quantize(Decimal("0.01"))
            except InvalidOperation:
                return None, f"invalid credit_limit '{raw_limit}'"
            if exact != limit:
                return None, f"credit_limit '{raw_limit}' has more than 2 decimal places"
            if exact < 0 or exact >= MAX_CREDIT_LIMIT:
                return None, f"credit_limit '{raw_limit}' is out of range"
            values["credit_limit"] = exact

        return values, None
