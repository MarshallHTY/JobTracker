from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from job_url_import import (
    ExtractedJob,
    JobUrlImportError,
    extract_job_from_html_file,
    extract_job_from_url,
)

DB_PATH = Path(__file__).with_name("job_applications.db")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

VALID_STATUSES = {
    "saved",
    "drafting",
    "applied",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "ghosted",
    "withdrawn",
}

VALID_SPONSORSHIP = {"yes", "no", "unknown"}

# Column order for CSV (matches applications table)
CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "company",
    "role_title",
    "role_type",
    "location",
    "visa_sponsorship",
    "source",
    "job_link",
    "date_found",
    "date_applied",
    "apply_by_date",
    "status",
    "cv_version",
    "cl_version",
    "follow_up_date",
    "priority",
    "fit_score",
    "notes",
    "created_at",
    "updated_at",
)


class JobTracker:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(schema)
            try:
                conn.execute(
                    "ALTER TABLE applications ADD COLUMN apply_by_date TEXT"
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def add_application(
        self,
        company: str,
        role_title: str,
        role_type: str | None = None,
        location: str | None = None,
        visa_sponsorship: str = "unknown",
        source: str | None = None,
        job_link: str | None = None,
        date_found: str | None = None,
        date_applied: str | None = None,
        apply_by_date: str | None = None,
        status: str = "saved",
        cv_version: str | None = None,
        cl_version: str | None = None,
        follow_up_date: str | None = None,
        priority: int = 3,
        fit_score: float | None = None,
        notes: str | None = None,
    ) -> int:
        self._validate_status(status)
        self._validate_sponsorship(visa_sponsorship)

        query = """
        INSERT INTO applications (
            company, role_title, role_type, location, visa_sponsorship,
            source, job_link, date_found, date_applied, apply_by_date, status,
            cv_version, cl_version, follow_up_date, priority, fit_score, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            company,
            role_title,
            role_type,
            location,
            visa_sponsorship,
            source,
            job_link,
            date_found,
            date_applied,
            apply_by_date,
            status,
            cv_version,
            cl_version,
            follow_up_date,
            priority,
            fit_score,
            notes,
        )
        with self._connect() as conn:
            cursor = conn.execute(query, values)
            conn.commit()
            return int(cursor.lastrowid)

    def update_status(self, application_id: int, status: str) -> None:
        self._validate_status(status)
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (status, application_id),
            )
            conn.commit()
            if result.rowcount == 0:
                raise ValueError(f"No application found with id={application_id}")

    def update_follow_up(self, application_id: int, follow_up_date: str) -> None:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE applications SET follow_up_date = ? WHERE id = ?",
                (follow_up_date, application_id),
            )
            conn.commit()
            if result.rowcount == 0:
                raise ValueError(f"No application found with id={application_id}")

    def update_apply_by_date(self, application_id: int, apply_by_date: str) -> None:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE applications SET apply_by_date = ? WHERE id = ?",
                (apply_by_date, application_id),
            )
            conn.commit()
            if result.rowcount == 0:
                raise ValueError(f"No application found with id={application_id}")

    def list_applications(self, status: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM applications"
        params: tuple[Any, ...] = ()
        if status:
            self._validate_status(status)
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY COALESCE(date_applied, date_found, created_at) DESC, id DESC"

        with self._connect() as conn:
            return list(conn.execute(query, params).fetchall())

    def search_by_company(self, keyword: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM applications
                    WHERE company LIKE ?
                    ORDER BY COALESCE(date_applied, date_found, created_at) DESC, id DESC
                    """,
                    (f"%{keyword}%",),
                ).fetchall()
            )

    def get_due_follow_ups(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM applications
                    WHERE follow_up_date IS NOT NULL
                      AND follow_up_date <= date('now')
                      AND status IN ('applied', 'assessment', 'interview')
                    ORDER BY follow_up_date ASC, priority DESC
                    """
                ).fetchall()
            )

    def get_summary(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM applications
                    GROUP BY status
                    ORDER BY count DESC, status ASC
                    """
                ).fetchall()
            )

    def export_csv(self, output_path: Path, status: str | None = None) -> int:
        rows = self.list_applications(status=status)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row[name] for name in CSV_COLUMNS})
        return len(rows)

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status '{status}'. Valid values: {sorted(VALID_STATUSES)}"
            )

    @staticmethod
    def _validate_sponsorship(value: str) -> None:
        if value not in VALID_SPONSORSHIP:
            raise ValueError(
                f"Invalid visa_sponsorship '{value}'. Valid values: {sorted(VALID_SPONSORSHIP)}"
            )


def print_rows(rows: Iterable[sqlite3.Row]) -> None:
    rows = list(rows)
    if not rows:
        print("No records found.")
        return

    for row in rows:
        print(
            f"[{row['id']}] {row['company']} | {row['role_title']} | "
            f"status={row['status']} | location={row['location'] or '-'} | "
            f"apply_by={row['apply_by_date'] or '-'} | "
            f"applied={row['date_applied'] or '-'} | follow_up={row['follow_up_date'] or '-'}"
        )
        if row["notes"]:
            print(f"    notes: {row['notes']}")


def _print_extracted_preview(ex: ExtractedJob) -> None:
    print("Extracted from page:")
    print(f"  company:       {ex.company}")
    print(f"  role_title:    {ex.role_title}")
    print(f"  role_type:     {ex.role_type or '-'}")
    print(f"  location:      {ex.location or '-'}")
    print(f"  apply_by_date: {ex.apply_by_date or '-'}")
    print(f"  date_found:    {ex.date_found or '-'} (from posting date or today)")
    print(f"  source:        {ex.source or '-'}")
    if ex.notes:
        preview = ex.notes[:400] + ("…" if len(ex.notes) > 400 else "")
        print(f"  notes preview: {preview}")
    else:
        print("  notes:         -")
    if ex.hint:
        print()
        print(f"  Note: {ex.hint}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track job applications with SQLite.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new application")
    add_parser.add_argument("--company", required=True)
    add_parser.add_argument("--role-title", required=True)
    add_parser.add_argument("--role-type")
    add_parser.add_argument("--location")
    add_parser.add_argument("--visa-sponsorship", default="unknown")
    add_parser.add_argument("--source")
    add_parser.add_argument("--job-link")
    add_parser.add_argument("--date-found")
    add_parser.add_argument("--date-applied")
    add_parser.add_argument(
        "--apply-by-date",
        help="Last day to apply (use YYYY-MM-DD, same as other dates)",
    )
    add_parser.add_argument("--status", default="saved")
    add_parser.add_argument("--cv-version")
    add_parser.add_argument("--cl-version")
    add_parser.add_argument("--follow-up-date")
    add_parser.add_argument("--priority", type=int, default=3)
    add_parser.add_argument("--fit-score", type=float)
    add_parser.add_argument("--notes")

    from_url = subparsers.add_parser(
        "add-from-url",
        help="Fetch a job posting URL and fill company, title, location, dates, notes from the page",
    )
    from_url.add_argument("url", help="Job posting page URL (saved as job link)")
    from_url.add_argument(
        "--dry-run",
        action="store_true",
        help="Print extracted fields only; do not write to the database",
    )
    from_url.add_argument(
        "--html-file",
        metavar="PATH",
        help="Read HTML from a saved file instead of fetching (e.g. after Save As from a browser)",
    )
    from_url.add_argument(
        "--no-notes",
        action="store_true",
        help="Do not store description text in notes (still saves title, company, etc.)",
    )
    from_url.add_argument("--company", help="Override extracted company name")
    from_url.add_argument("--role-title", help="Override extracted job title")
    from_url.add_argument("--role-type")
    from_url.add_argument("--location")
    from_url.add_argument("--visa-sponsorship", default="unknown")
    from_url.add_argument("--source", help="Override inferred site/source")
    from_url.add_argument("--date-found")
    from_url.add_argument("--date-applied")
    from_url.add_argument("--apply-by-date")
    from_url.add_argument("--status", default="saved")
    from_url.add_argument("--cv-version")
    from_url.add_argument("--cl-version")
    from_url.add_argument("--follow-up-date")
    from_url.add_argument("--priority", type=int, default=3)
    from_url.add_argument("--fit-score", type=float)
    from_url.add_argument(
        "--notes",
        help="Replace extracted notes; omit to use trimmed description from the page",
    )

    status_parser = subparsers.add_parser("update-status", help="Update application status")
    status_parser.add_argument("id", type=int)
    status_parser.add_argument("status")

    followup_parser = subparsers.add_parser("set-followup", help="Set follow-up date")
    followup_parser.add_argument("id", type=int)
    followup_parser.add_argument("follow_up_date")

    apply_by_parser = subparsers.add_parser(
        "set-apply-by", help="Set application (apply-by) deadline"
    )
    apply_by_parser.add_argument("id", type=int)
    apply_by_parser.add_argument("apply_by_date")

    list_parser = subparsers.add_parser("list", help="List applications")
    list_parser.add_argument("--status")

    company_parser = subparsers.add_parser("search-company", help="Search company by keyword")
    company_parser.add_argument("keyword")

    subparsers.add_parser("followups", help="Show due follow-ups")
    subparsers.add_parser("summary", help="Show summary counts")

    export_csv_parser = subparsers.add_parser(
        "export-csv",
        help="Export all applications to a CSV file (UTF-8 with BOM for Excel)",
    )
    export_csv_parser.add_argument(
        "-o",
        "--output",
        default="job_applications_export.csv",
        help="Output file path (default: job_applications_export.csv next to the script)",
    )
    export_csv_parser.add_argument(
        "--status",
        help="Only export rows with this status",
    )

    return parser


def main() -> None:
    tracker = JobTracker()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "add":
        app_id = tracker.add_application(
            company=args.company,
            role_title=args.role_title,
            role_type=args.role_type,
            location=args.location,
            visa_sponsorship=args.visa_sponsorship,
            source=args.source,
            job_link=args.job_link,
            date_found=args.date_found,
            date_applied=args.date_applied,
            apply_by_date=args.apply_by_date,
            status=args.status,
            cv_version=args.cv_version,
            cl_version=args.cl_version,
            follow_up_date=args.follow_up_date,
            priority=args.priority,
            fit_score=args.fit_score,
            notes=args.notes,
        )
        print(f"Added application with id={app_id}")

    elif args.command == "add-from-url":
        try:
            if args.html_file:
                ex = extract_job_from_html_file(args.html_file, args.url)
            else:
                ex = extract_job_from_url(args.url)
        except JobUrlImportError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise SystemExit(1) from e

        if args.no_notes:
            notes_val: str | None = None
        elif args.notes is not None:
            notes_val = args.notes
        else:
            notes_val = ex.notes

        if args.dry_run:
            _print_extracted_preview(ex)
            print()
            print("Would save (with overrides applied):")
            print(f"  company:       {args.company or ex.company}")
            print(f"  role_title:    {args.role_title or ex.role_title}")
            print(f"  role_type:     {args.role_type or ex.role_type or '-'}")
            print(f"  location:      {args.location or ex.location or '-'}")
            print(f"  apply_by_date: {args.apply_by_date or ex.apply_by_date or '-'}")
            print(f"  date_found:    {args.date_found or ex.date_found or '-'}")
            print(f"  job_link:      {args.url}")
            print(f"  notes:         {'(none)' if not notes_val else '(set)'}")
            return

        app_id = tracker.add_application(
            company=args.company or ex.company,
            role_title=args.role_title or ex.role_title,
            role_type=args.role_type or ex.role_type,
            location=args.location or ex.location,
            visa_sponsorship=args.visa_sponsorship,
            source=args.source or ex.source,
            job_link=args.url,
            date_found=args.date_found or ex.date_found,
            date_applied=args.date_applied,
            apply_by_date=args.apply_by_date or ex.apply_by_date,
            status=args.status,
            cv_version=args.cv_version,
            cl_version=args.cl_version,
            follow_up_date=args.follow_up_date,
            priority=args.priority,
            fit_score=args.fit_score,
            notes=notes_val,
        )
        _print_extracted_preview(ex)
        print()
        print(f"Added application with id={app_id}")

    elif args.command == "update-status":
        tracker.update_status(args.id, args.status)
        print(f"Updated id={args.id} to status='{args.status}'")

    elif args.command == "set-followup":
        tracker.update_follow_up(args.id, args.follow_up_date)
        print(f"Set follow-up date for id={args.id} to {args.follow_up_date}")

    elif args.command == "set-apply-by":
        tracker.update_apply_by_date(args.id, args.apply_by_date)
        print(f"Set apply-by date for id={args.id} to {args.apply_by_date}")

    elif args.command == "list":
        print_rows(tracker.list_applications(status=args.status))

    elif args.command == "search-company":
        print_rows(tracker.search_by_company(args.keyword))

    elif args.command == "followups":
        print_rows(tracker.get_due_follow_ups())

    elif args.command == "summary":
        rows = tracker.get_summary()
        if not rows:
            print("No records found.")
        for row in rows:
            print(f"{row['status']}: {row['count']}")

    elif args.command == "export-csv":
        out = Path(args.output)
        if not out.is_absolute():
            out = Path(__file__).resolve().parent / out
        n = tracker.export_csv(out, status=args.status)
        print(f"Wrote {n} row(s) to {out}")


if __name__ == "__main__":
    main()
