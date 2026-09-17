import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from outreach.models import Profile, Prospect


@dataclass(frozen=True)
class SourceList:
    label: str
    filename: str
    workstreams: tuple[str, ...]


SOURCE_LISTS = (
    SourceList(
        label="Florida and Chicago target firms",
        filename="florida_chicago.csv",
        workstreams=(Prospect.Workstream.INTERN, Prospect.Workstream.LINKEDIN_OUTREACH),
    ),
    SourceList(
        label="New York, Massachusetts and Connecticut target firms",
        filename="ny_ma_ct.csv",
        workstreams=(Prospect.Workstream.INSIDE_SALES, Prospect.Workstream.LINKEDIN_OUTREACH),
    ),
)


def normalized_host(website):
    parsed = urlsplit(website)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return (parsed.hostname or "").lower().removeprefix("www.")


class Command(BaseCommand):
    help = "Import the approved target-firm lists into unassigned outreach workstream queues."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command performs a rolled-back rehearsal.",
        )
        parser.add_argument(
            "--created-by-email",
            help="System administrator email used for the import audit trail.",
        )

    def handle(self, *args, **options):
        creator = self.get_creator(options.get("created_by_email"))
        source_rows = self.load_source_rows()
        stats = Counter()

        with transaction.atomic():
            existing_by_key = self.existing_prospects_by_key()
            for source, row_number, company_name, website, area, import_key in source_rows:
                for workstream in source.workstreams:
                    key = (workstream, import_key)
                    existing = existing_by_key.get(key)
                    if existing:
                        changed_fields = []
                        if not existing.import_key:
                            existing.import_key = import_key
                            changed_fields.append("import_key")
                        if not existing.import_source:
                            existing.import_source = source.label
                            changed_fields.append("import_source")
                        if not existing.location:
                            existing.location = area
                            changed_fields.append("location")
                        if changed_fields:
                            changed_fields.append("updated_at")
                            existing.save(update_fields=changed_fields)
                            stats[f"updated:{workstream}"] += 1
                        else:
                            stats[f"existing:{workstream}"] += 1
                        continue

                    prospect = Prospect(
                        owner=None,
                        workstream=workstream,
                        stage=Prospect.Stage.ELIGIBLE,
                        company_name=company_name,
                        website=website,
                        short_description="",
                        location=area,
                        contact_name="",
                        status=Prospect.Status.NOT_RESPONDED,
                        import_source=source.label,
                        import_key=import_key,
                        comments=f"Imported from {source.label}, source row {row_number}.",
                        created_by=creator,
                    )
                    prospect.full_clean()
                    prospect.save()
                    existing_by_key[key] = prospect
                    stats[f"created:{workstream}"] += 1

            total_after = {
                workstream: Prospect.objects.filter(
                    workstream=workstream,
                    import_key__gt="",
                ).count()
                for workstream in Prospect.Workstream.values
            }
            if not options["apply"]:
                transaction.set_rollback(True)

        mode = "APPLIED" if options["apply"] else "DRY RUN"
        self.stdout.write(self.style.SUCCESS(f"Target-firm import {mode}"))
        for workstream, label in Prospect.Workstream.choices:
            self.stdout.write(
                f"{label}: created={stats[f'created:{workstream}']} "
                f"updated={stats[f'updated:{workstream}']} "
                f"existing={stats[f'existing:{workstream}']} "
                f"imported_total={total_after[workstream]}"
            )
        self.stdout.write(f"Source rows validated: {len(source_rows)}")
        self.stdout.write(f"Audit user: {creator.email}")

    def get_creator(self, email):
        users = get_user_model().objects.filter(
            is_active=True,
            profile__role=Profile.Role.SYSTEM_ADMIN,
        )
        if email:
            try:
                return users.get(email__iexact=email.strip())
            except get_user_model().DoesNotExist as error:
                raise CommandError(f"No active system administrator exists for {email}.") from error

        candidates = list(users.order_by("email"))
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise CommandError("Create an active system administrator before importing target firms.")
        emails = ", ".join(user.email for user in candidates)
        raise CommandError(
            "More than one active system administrator exists. "
            f"Pass --created-by-email with one of: {emails}"
        )

    def load_source_rows(self):
        data_dir = Path(settings.BASE_DIR) / "data" / "target_firms"
        source_rows = []
        seen_domains = {}
        expected_header = ["Company Name", "Website", "Area"]

        for source in SOURCE_LISTS:
            path = data_dir / source.filename
            if not path.is_file():
                raise CommandError(f"Target-firm source file is missing: {path}")
            with path.open(newline="", encoding="utf-8-sig") as source_file:
                reader = csv.reader(source_file)
                header = next(reader, None)
                if header != expected_header:
                    raise CommandError(
                        f"Unexpected header in {source.filename}: {header!r}; expected {expected_header!r}."
                    )
                for row_number, row in enumerate(reader, 2):
                    if len(row) != 3 or not all(value.strip() for value in row):
                        raise CommandError(f"Incomplete row in {source.filename} at row {row_number}: {row!r}")
                    company_name, website, area = (value.strip() for value in row)
                    import_key = normalized_host(website)
                    if not import_key:
                        raise CommandError(f"Invalid website in {source.filename} at row {row_number}: {website}")
                    previous = seen_domains.get(import_key)
                    if previous:
                        raise CommandError(
                            f"Duplicate website domain {import_key} in {source.filename} row {row_number}; "
                            f"first seen in {previous}."
                        )
                    seen_domains[import_key] = f"{source.filename} row {row_number}"
                    source_rows.append((source, row_number, company_name, website, area, import_key))
        return source_rows

    def existing_prospects_by_key(self):
        existing = {}
        for prospect in Prospect.objects.only(
            "id",
            "workstream",
            "website",
            "import_key",
            "import_source",
            "location",
        ):
            key = prospect.import_key or normalized_host(prospect.website)
            if not key:
                continue
            workstream_key = (prospect.workstream, key)
            if workstream_key in existing:
                raise CommandError(
                    f"Existing duplicate domain {key} in the {prospect.get_workstream_display()} workstream."
                )
            existing[workstream_key] = prospect
        return existing
