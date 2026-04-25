# Job Application Tracker

A lightweight Python + SQLite CLI to track job applications, deadlines, and follow-ups.

## Project structure

- `app_tracker.py`: main CLI entry point (add/list/update/export commands)
- `job_url_import.py`: URL/HTML parser for auto-filling job fields from vacancy pages
- `schema.sql`: SQLite schema and trigger setup
- `job_applications.db`: created automatically on first run (local data)

## Requirements

- Python 3.10+
- No third-party libraries required (standard library only)

## Quick start

```bash
python app_tracker.py add \
  --company "Sony Interactive Entertainment" \
  --role-title "Data Scientist - Experimentation & Measurement" \
  --location "London" \
  --visa-sponsorship unknown \
  --date-found 2026-04-04 \
  --status saved \
  --priority 5 \
  --notes "Need tailored CV with experimentation emphasis"
```

```bash
python app_tracker.py list
python app_tracker.py summary
python app_tracker.py followups
```

## Example commands

Update status:
```bash
python app_tracker.py update-status 1 applied
```

Set follow-up date:
```bash
python app_tracker.py set-followup 1 2026-04-11
```

Set apply-by date:
```bash
python app_tracker.py set-apply-by 1 2026-05-03
```

Import from a job URL:
```bash
python app_tracker.py add-from-url "https://example.com/job-posting"
```

List only interviews:
```bash
python app_tracker.py list --status interview
```

Search by company:
```bash
python app_tracker.py search-company Sony
```

Export to CSV:
```bash
python app_tracker.py export-csv --output job_applications_export.csv
```

## Potential next upgrades

1. Add JD parsing with an LLM fallback for messy closing-date formats (Currently Working On)
2. Add auto-tagging for DS / DA / SWE / ML roles
