from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT_SEC = 25
MAX_NOTES_LEN = 2500


class JobUrlImportError(Exception):
    """Raised when the page cannot be fetched or parsed into job fields."""


@dataclass(frozen=True)
class ExtractedJob:
    company: str
    role_title: str
    role_type: str | None
    location: str | None
    apply_by_date: str | None
    date_found: str | None
    job_link: str
    notes: str | None
    source: str | None
    hint: str | None = None


def _normalize_iso_date(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    t = re.sub(r"(?s)<script[^>]*>.*?</script>", " ", text, flags=re.I)
    t = re.sub(r"(?s)<style[^>]*>.*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _meta_content(html: str, *, prop: str | None = None, name: str | None = None) -> str | None:
    if prop:
        m = re.search(
            r'<meta[^>]+property=["\']'
            + re.escape(prop)
            + r'["\'][^>]+content=["\']([^"\']*)["\']',
            html,
            re.I,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\''
            + re.escape(prop)
            + r'["\']',
            html,
            re.I,
        )
    elif name:
        m = re.search(
            r'<meta[^>]+name=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\']([^"\']*)["\']',
            html,
            re.I,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\''
            + re.escape(name)
            + r'["\']',
            html,
            re.I,
        )
    else:
        return None
    return unescape(m.group(1)).strip() if m else None


def _title_tag(html: str) -> str | None:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I | re.DOTALL)
    if not m:
        return None
    return unescape(re.sub(r"\s+", " ", m.group(1)).strip())


def _hostname_source(url: str) -> str | None:
    host = urlparse(url).hostname
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def _flatten_json_ld(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "@graph" in node:
            out.extend(_flatten_json_ld(node["@graph"]))
        else:
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            out.extend(_flatten_json_ld(item))
    return out


def _job_posting_types(t: Any) -> list[str]:
    if t is None:
        return []
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


def _org_name(org: Any) -> str | None:
    if isinstance(org, str):
        return org.strip() or None
    if isinstance(org, dict):
        name = org.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _location_string(loc: Any) -> str | None:
    if loc is None:
        return None
    if isinstance(loc, str):
        return loc.strip() or None
    if isinstance(loc, dict):
        if loc.get("@type") == "Place" and isinstance(loc.get("name"), str):
            return loc["name"].strip()
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("addressCountry"),
            ]
            bits = [str(p).strip() for p in parts if p]
            if bits:
                return ", ".join(bits)
        if isinstance(loc.get("name"), str):
            return loc["name"].strip()
    return None


def _employment_type(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, list) and val:
        return ", ".join(str(x) for x in val if x)
    return None


def _best_job_posting(objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for obj in objects:
        types = _job_posting_types(obj.get("@type"))
        if "JobPosting" in types:
            candidates.append(obj)
    if not candidates:
        return None

    def score(obj: dict[str, Any]) -> int:
        s = 0
        if obj.get("title"):
            s += 2
        if obj.get("hiringOrganization"):
            s += 2
        if obj.get("description"):
            s += 1
        if obj.get("validThrough"):
            s += 1
        if obj.get("jobLocation"):
            s += 1
        return s

    return max(candidates, key=score)


def _parse_from_json_ld(html: str) -> dict[str, Any] | None:
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.DOTALL,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        flat = _flatten_json_ld(data)
        jp = _best_job_posting(flat)
        if jp:
            return jp
    return None


def _deep_find_job_postings(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        types = _job_posting_types(obj.get("@type"))
        if "JobPosting" in types:
            found.append(obj)
        for v in obj.values():
            found.extend(_deep_find_job_postings(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_deep_find_job_postings(item))
    return found


def _merge_job_posting_dicts(postings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not postings:
        return None
    if len(postings) == 1:
        return postings[0]
    merged: dict[str, Any] = {}
    for p in postings:
        for k, v in p.items():
            if v is None or v == "":
                continue
            if k not in merged or merged[k] in (None, ""):
                merged[k] = v
    return merged


def _parse_from_embedded_json_scripts(html: str) -> dict[str, Any] | None:
    """Pick up JobPosting blobs inside any <script> JSON (e.g. some ATS / BambooHR embeds)."""
    collected: list[dict[str, Any]] = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.I | re.DOTALL):
        raw = m.group(1).strip()
        if len(raw) < 30:
            continue
        if raw.startswith("<!--"):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _deep_find_job_postings(data)
        collected.extend(found)
    if not collected:
        return None
    return _best_job_posting(collected)


def _closing_date_from_posting(jp: dict[str, Any]) -> str | None:
    for key in (
        "validThrough",
        "applicationDeadline",
        "expirationDate",
        "closingDate",
    ):
        v = jp.get(key)
        if isinstance(v, str):
            d = _normalize_iso_date(v)
            if d:
                return d
    return None


_MONTH_NAME_TO_NUM: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# "24th April" / "24 April 2026"
_RE_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s*[,.]?\s*(\d{4}))?\b",
    re.I,
)
# "April 24" / "April 24th, 2026"
_RE_MONTH_DAY = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*[,.]?\s*(\d{4}))?\b",
    re.I,
)
_RE_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
# Phrases that often precede a human-readable deadline in job ads
_RE_DEADLINE_HINT = re.compile(
    r"(?is)\b(?:"
    r"applications?\s+will\s+close|application\s+deadline|closing\s+date|"
    r"closes?\s+on|close(?:s|d)?\s+(?:at|on|by)|"
    r"must\s+be\s+received\s+by|must\s+apply\s+by|"
    r"apply\s+(?:online\s+)?by|submit\s+(?:your\s+)?application\s+by|"
    r"last\s+day\s+to\s+apply|final\s+date\s+for\s+applications|"
    r"vacancy\s+closes|posting\s+closes|role\s+closes|"
    r"\bdeadline\s*(?:is|of|:)?"
    r")\b",
)
# UK HE / council style: "Closing Date" then (optional weekday) "17 April 2026"
_RE_AFTER_CLOSING_DATE_LABEL = re.compile(
    r"(?is)Closing\s+Date\s*"
    r".{0,320}?"
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?\b",
)
# Same idea when the label is on its own line (common on .ac.uk job systems)
_RE_AFTER_APP_DEADLINE_LABEL = re.compile(
    r"(?is)Application\s+Deadline\s*"
    r".{0,320}?"
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?\b",
)


def _date_from_parts(year: int | None, month: int, day: int) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
    except ValueError:
        return None
    today = date.today()
    try:
        cand = date(today.year, month, day)
    except ValueError:
        return None
    if cand >= today:
        return cand
    try:
        cand_next = date(today.year + 1, month, day)
    except ValueError:
        return None
    return cand_next


def _parse_iso_in_window(s: str) -> date | None:
    m = _RE_ISO_DATE.search(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_natural_date_in_window(s: str) -> date | None:
    m = _RE_DAY_MONTH.search(s)
    if m:
        day = int(m.group(1))
        month = _MONTH_NAME_TO_NUM[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else None
        return _date_from_parts(year, month, day)
    m = _RE_MONTH_DAY.search(s)
    if m:
        month = _MONTH_NAME_TO_NUM[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        return _date_from_parts(year, month, day)
    return None


def _groups_day_month_to_date(m: re.Match[str]) -> date | None:
    day = int(m.group(1))
    month = _MONTH_NAME_TO_NUM[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else None
    return _date_from_parts(year, month, day)


def _deadline_from_prose(text: str) -> str | None:
    """
    When structured JobPosting.validThrough is missing, infer apply-by from
    phrases like 'Applications will close on 24th April' or labelled
    'Closing Date' blocks (e.g. many UK university vacancy pages).
    """
    if not text:
        return None
    snippet = text[:32000]

    for m in _RE_DEADLINE_HINT.finditer(snippet):
        window = snippet[m.end() : m.end() + 420]
        iso_d = _parse_iso_in_window(window)
        if iso_d:
            return iso_d.isoformat()
        nat = _parse_natural_date_in_window(window)
        if nat:
            return nat.isoformat()

    for rx in (_RE_AFTER_CLOSING_DATE_LABEL, _RE_AFTER_APP_DEADLINE_LABEL):
        m = rx.search(snippet)
        if m:
            d = _groups_day_month_to_date(m)
            if d:
                return d.isoformat()

    return None


def _merge_ld_and_embedded(
    ld: dict[str, Any] | None, embedded: dict[str, Any] | None
) -> dict[str, Any] | None:
    if ld and embedded:
        return _merge_job_posting_dicts([ld, embedded])
    return ld or embedded


def _split_title_pipe(title: str) -> tuple[str, str | None]:
    if "|" not in title:
        return title.strip(), None
    parts = [p.strip() for p in title.split("|") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return title.strip(), None


def extract_from_html(html: str, page_url: str) -> ExtractedJob:
    jp_ld = _parse_from_json_ld(html)
    jp_embed = _parse_from_embedded_json_scripts(html)
    jp = _merge_ld_and_embedded(jp_ld, jp_embed)
    role_title: str | None = None
    company: str | None = None
    role_type: str | None = None
    location: str | None = None
    apply_by: str | None = None
    date_posted: str | None = None
    notes: str | None = None

    if jp:
        role_title = (jp.get("title") or "").strip() or None
        company = _org_name(jp.get("hiringOrganization"))
        role_type = _employment_type(jp.get("employmentType"))
        location = _location_string(jp.get("jobLocation"))
        apply_by = _closing_date_from_posting(jp)
        date_posted = _normalize_iso_date(
            jp.get("datePosted") if isinstance(jp.get("datePosted"), str) else None
        )
        desc = jp.get("description")
        if isinstance(desc, str) and desc.strip():
            plain = _strip_html(desc)
            if len(plain) > MAX_NOTES_LEN:
                plain = plain[: MAX_NOTES_LEN - 1].rstrip() + "…"
            notes = plain or None

    og_title = _meta_content(html, prop="og:title")
    og_site = _meta_content(html, prop="og:site_name")
    doc_title = _title_tag(html)

    if not role_title:
        if og_title:
            role_title, guess_company = _split_title_pipe(og_title)
            if not company and guess_company:
                company = guess_company
        elif doc_title:
            role_title, guess_company = _split_title_pipe(doc_title)
            if not company and guess_company:
                company = guess_company
        else:
            role_title = None

    if not company:
        company = og_site
    if not company:
        host = _hostname_source(page_url)
        company = host or "Unknown company"

    if not role_title:
        role_title = doc_title or og_title or "Untitled role"

    source = _hostname_source(page_url)

    date_found = date_posted or date.today().isoformat()

    visible_plain = _strip_html(html)
    if not apply_by:
        corpus = "\n".join(x for x in (notes, visible_plain) if x)
        guessed = _deadline_from_prose(corpus)
        if guessed:
            apply_by = guessed

    hint: str | None = None
    if not apply_by and len(html) > 3500 and "closing date" not in html.lower():
        hint = (
            "No closing date found in this HTML. Some job portals (e.g. ASP.NET / .ac.uk) "
            "only put salary, location, and 'Closing Date' in the response your browser "
            "gets after cookies or scripts run. Save the vacancy page as HTML from your "
            "browser, then: add-from-url <same URL> --html-file path\\to\\saved.html"
        )

    return ExtractedJob(
        company=company,
        role_title=role_title.strip(),
        role_type=role_type,
        location=location,
        apply_by_date=apply_by,
        date_found=date_found,
        job_link=page_url,
        notes=notes,
        source=source,
        hint=hint,
    )


def fetch_html(url: str) -> str:
    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    ctx = ssl.create_default_context()
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT_SEC, context=ctx) as resp:
            raw = resp.read()
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            return raw.decode(charset, errors="replace")
    except HTTPError as e:
        raise JobUrlImportError(f"HTTP error {e.code} for URL") from e
    except URLError as e:
        raise JobUrlImportError(f"Could not fetch URL: {e.reason}") from e


def extract_job_from_url(url: str) -> ExtractedJob:
    if not url.strip():
        raise JobUrlImportError("URL is empty.")
    html = fetch_html(url)
    if len(html) < 200:
        raise JobUrlImportError(
            "Page body is very short; the site may require login or block automated access."
        )
    return extract_from_html(html, url.strip())


def extract_job_from_html_file(path: str, original_url: str) -> ExtractedJob:
    from pathlib import Path

    p = Path(path)
    html = p.read_text(encoding="utf-8", errors="replace")
    return extract_from_html(html, original_url.strip())
