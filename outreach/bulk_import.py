import csv
from dataclasses import dataclass
from io import StringIO
from pathlib import PurePath
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Prospect, ProspectImportBatch


EXPECTED_HEADERS = ("Company Name", "Website", "Area")
MAX_CSV_SIZE_BYTES = 5 * 1024 * 1024


class BulkImportFileError(ValueError):
    pass


@dataclass(frozen=True)
class BulkImportRow:
    row_number: int
    company_name: str
    website: str
    area: str
    domain: str


def normalized_company_name(value):
    return " ".join(value.split()).casefold()


def normalized_website_domain(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return (parsed.hostname or "").lower().removeprefix("www.")


def _skipped_row(row_number, company_name="", website="", area="", reason=""):
    return {
        "row_number": row_number,
        "company_name": company_name,
        "website": website,
        "area": area,
        "reason": reason,
    }


def parse_bulk_prospect_csv(uploaded_file):
    if uploaded_file.size > MAX_CSV_SIZE_BYTES:
        raise BulkImportFileError("The CSV must be 5 MB or smaller.")
    try:
        csv_text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise BulkImportFileError("Save the CSV with UTF-8 encoding and upload it again.") from error
    if not csv_text.strip():
        raise BulkImportFileError("The CSV is empty.")
    if "\x00" in csv_text:
        raise BulkImportFileError("The CSV contains unsupported null characters.")

    reader = csv.reader(StringIO(csv_text, newline=""))
    try:
        header = next(reader, None)
    except csv.Error as error:
        raise BulkImportFileError(f"The CSV header could not be read: {error}.") from error
    normalized_header = tuple(value.strip() for value in (header or []))
    if normalized_header != EXPECTED_HEADERS:
        raise BulkImportFileError(
            "Use exactly these three columns in this order: Company Name, Website, Area."
        )

    rows = []
    skipped_rows = []
    seen_domains = {}
    seen_company_names = {}
    total_rows = 0
    url_validator = URLValidator(schemes=["http", "https"])

    try:
        for row_number, raw_row in enumerate(reader, 2):
            total_rows += 1
            values = [value.strip() for value in raw_row[:3]]
            values.extend([""] * (3 - len(values)))
            company_name, website, area = values

            if len(raw_row) != 3:
                skipped_rows.append(
                    _skipped_row(
                        row_number,
                        company_name,
                        website,
                        area,
                        f"Expected 3 columns but found {len(raw_row)}.",
                    )
                )
                continue

            missing_headers = [
                header_name
                for header_name, value in zip(EXPECTED_HEADERS, values)
                if not value
            ]
            if missing_headers:
                skipped_rows.append(
                    _skipped_row(
                        row_number,
                        company_name,
                        website,
                        area,
                        f"Missing required value(s): {', '.join(missing_headers)}.",
                    )
                )
                continue

            validation_errors = []
            if len(company_name) > Prospect._meta.get_field("company_name").max_length:
                validation_errors.append("Company Name is longer than 200 characters")
            if len(website) > Prospect._meta.get_field("website").max_length:
                validation_errors.append("Website is longer than 500 characters")
            if len(area) > Prospect._meta.get_field("location").max_length:
                validation_errors.append("Area is longer than 200 characters")
            try:
                url_validator(website)
            except ValidationError:
                validation_errors.append("Website must be a complete http:// or https:// URL")
            domain = normalized_website_domain(website)
            if not domain:
                validation_errors.append("Website must contain a valid domain")
            if validation_errors:
                skipped_rows.append(
                    _skipped_row(
                        row_number,
                        company_name,
                        website,
                        area,
                        "; ".join(dict.fromkeys(validation_errors)) + ".",
                    )
                )
                continue

            company_key = normalized_company_name(company_name)
            if domain in seen_domains:
                skipped_rows.append(
                    _skipped_row(
                        row_number,
                        company_name,
                        website,
                        area,
                        f"Duplicate Website in this CSV; first seen on row {seen_domains[domain]}.",
                    )
                )
                continue
            if company_key in seen_company_names:
                skipped_rows.append(
                    _skipped_row(
                        row_number,
                        company_name,
                        website,
                        area,
                        "Duplicate Company Name in this CSV; "
                        f"first seen on row {seen_company_names[company_key]}.",
                    )
                )
                continue

            seen_domains[domain] = row_number
            seen_company_names[company_key] = row_number
            rows.append(BulkImportRow(row_number, company_name, website, area, domain))
    except csv.Error as error:
        raise BulkImportFileError(f"The CSV could not be read: {error}.") from error

    if total_rows == 0:
        raise BulkImportFileError("The CSV contains a header but no prospect rows.")
    return rows, skipped_rows, total_rows


def _batch_source_name(batch):
    return f"Bulk CSV: {batch.source_filename}"[:120]


def _batch_comment(batch, row_number):
    return (
        f"Imported from {batch.source_filename}, source row {row_number}, batch {batch.pk}."
    )


def import_prospects_from_csv(*, uploaded_file, uploaded_by, workstream, owner):
    parsed_rows, skipped_rows, total_rows = parse_bulk_prospect_csv(uploaded_file)
    source_filename = PurePath(uploaded_file.name or "prospects.csv").name[:255]

    with transaction.atomic():
        batch = ProspectImportBatch.objects.create(
            uploaded_by=uploaded_by,
            source_filename=source_filename,
            target_workstream=workstream,
            owner=owner,
            total_rows=total_rows,
        )
        existing_domains = {}
        existing_company_names = {}
        for prospect in Prospect.objects.filter(workstream=workstream).only(
            "id",
            "company_name",
            "website",
            "import_key",
        ):
            domain = prospect.import_key or normalized_website_domain(prospect.website)
            if domain:
                existing_domains.setdefault(domain, prospect)
            existing_company_names.setdefault(
                normalized_company_name(prospect.company_name),
                prospect,
            )

        created_rows = []
        import_source = _batch_source_name(batch)
        for row in parsed_rows:
            duplicate = existing_domains.get(row.domain)
            duplicate_reason = "Website"
            if duplicate is None:
                duplicate = existing_company_names.get(
                    normalized_company_name(row.company_name)
                )
                duplicate_reason = "Company Name"
            if duplicate is not None:
                skipped_rows.append(
                    _skipped_row(
                        row.row_number,
                        row.company_name,
                        row.website,
                        row.area,
                        f"Duplicate {duplicate_reason} in the {duplicate.get_workstream_display()} "
                        f"workstream; prospect #{duplicate.pk} is {duplicate.company_name}.",
                    )
                )
                continue

            comment = _batch_comment(batch, row.row_number)
            prospect = Prospect(
                owner=owner,
                workstream=workstream,
                stage=Prospect.Stage.ELIGIBLE,
                company_name=row.company_name,
                website=row.website,
                location=row.area,
                status=Prospect.Status.NOT_RESPONDED,
                import_batch=batch,
                import_source=import_source,
                import_key=row.domain,
                comments=comment,
                created_by=uploaded_by,
            )
            try:
                with transaction.atomic():
                    prospect.full_clean()
                    prospect.save()
            except (IntegrityError, ValidationError) as error:
                error_messages = getattr(error, "messages", None) or [str(error)]
                skipped_rows.append(
                    _skipped_row(
                        row.row_number,
                        row.company_name,
                        row.website,
                        row.area,
                        "The row could not be saved: " + "; ".join(error_messages),
                    )
                )
                continue

            snapshot = {
                "prospect_id": prospect.pk,
                "row_number": row.row_number,
                "company_name": row.company_name,
                "website": row.website,
                "area": row.area,
                "domain": row.domain,
                "import_source": import_source,
                "comments": comment,
            }
            created_rows.append(snapshot)
            existing_domains[row.domain] = prospect
            existing_company_names[normalized_company_name(row.company_name)] = prospect

        skipped_rows.sort(key=lambda item: item["row_number"])
        batch.created_count = len(created_rows)
        batch.skipped_count = len(skipped_rows)
        batch.created_rows = created_rows
        batch.skipped_rows = skipped_rows
        batch.save(
            update_fields=[
                "created_count",
                "skipped_count",
                "created_rows",
                "skipped_rows",
            ]
        )
    return batch


UNCHANGED_EMPTY_FIELDS = (
    "short_description",
    "consulting_focus",
    "client_segment",
    "eligibility_evidence",
    "contact_name",
    "contact_title",
    "contact_linkedin_url",
    "contact_email",
    "contact_phone",
    "founder_account",
    "linkedin_connection_status",
    "personalization_note",
    "founder_escalation_notes",
    "material_shared",
    "interest_signal",
    "questions_for_founders",
    "next_action",
    "meeting_timezone",
    "meeting_participants",
)


def _prospect_is_unchanged_since_import(prospect, batch, snapshot):
    if prospect.outreaches.exists() or prospect.update_audits.exists():
        return False
    expected_values = {
        "owner_id": batch.owner_id,
        "workstream": batch.target_workstream,
        "stage": Prospect.Stage.ELIGIBLE,
        "company_name": snapshot["company_name"],
        "website": snapshot["website"],
        "location": snapshot["area"],
        "status": Prospect.Status.NOT_RESPONDED,
        "import_batch_id": batch.pk,
        "import_source": snapshot["import_source"],
        "import_key": snapshot["domain"],
        "comments": snapshot["comments"],
        "created_by_id": batch.uploaded_by_id,
        "is_not_eligible": False,
        "prospect_sent": False,
        "founder_escalation_required": False,
        "next_action_date": None,
        "meeting_scheduled_at": None,
    }
    if any(getattr(prospect, field) != value for field, value in expected_values.items()):
        return False
    return all(not getattr(prospect, field) for field in UNCHANGED_EMPTY_FIELDS)


def rollback_prospect_import(*, batch_id, rolled_back_by):
    with transaction.atomic():
        batch = ProspectImportBatch.objects.select_for_update().get(pk=batch_id)
        if batch.status == ProspectImportBatch.Status.ROLLED_BACK:
            return batch, 0, []

        snapshots = {
            snapshot["prospect_id"]: snapshot for snapshot in batch.created_rows
        }
        current_prospects = list(
            batch.prospects.select_for_update().order_by("pk")
        )
        delete_ids = []
        protected_rows = []
        for prospect in current_prospects:
            snapshot = snapshots.get(prospect.pk)
            if snapshot and _prospect_is_unchanged_since_import(
                prospect,
                batch,
                snapshot,
            ):
                delete_ids.append(prospect.pk)
                continue
            protected_rows.append(
                _skipped_row(
                    snapshot["row_number"] if snapshot else 0,
                    prospect.company_name,
                    prospect.website,
                    prospect.location,
                    "Not removed because this prospect was modified or now has outreach/API activity.",
                )
                | {"prospect_id": prospect.pk}
            )

        if delete_ids:
            Prospect.objects.filter(pk__in=delete_ids).delete()
        remaining_count = batch.prospects.count()
        batch.rolled_back_count = batch.created_count - remaining_count
        batch.rollback_skipped_rows = protected_rows
        batch.rolled_back_at = timezone.now()
        batch.rolled_back_by = rolled_back_by
        batch.status = (
            ProspectImportBatch.Status.ROLLED_BACK
            if remaining_count == 0
            else ProspectImportBatch.Status.PARTIALLY_ROLLED_BACK
        )
        batch.save(
            update_fields=[
                "rolled_back_count",
                "rollback_skipped_rows",
                "rolled_back_at",
                "rolled_back_by",
                "status",
            ]
        )
    return batch, len(delete_ids), protected_rows
