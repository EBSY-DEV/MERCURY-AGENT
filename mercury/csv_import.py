"""Reading a CSV of contacts: decoding, delimiter, column mapping, row checks.

Pure functions with no database and no network, so a preview can never change
state or spend a credit. ``mercury.control.imports`` decides what each clean
row means against the database.

Limits are published (docs/prospecting.md) and enforced here: a file over
MAX_BYTES or MAX_ROWS is refused whole rather than half-imported.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field

from mercury.collectors.discover import is_junk, normalize_domain
from mercury.control.errors import ControlError

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_FIELD = 500
MAX_NOTES = 4000

DELIMITERS = {",": "comma", ";": "semicolon", "\t": "tab"}
DELIMITER_NAMES = {name: char for char, name in DELIMITERS.items()}

# Every field an import can fill, in the order the mapping UI lists them.
FIELDS = (
    "email", "first_name", "last_name", "full_name", "title", "company_name",
    "website", "industry", "linkedin_url", "phone", "personalization",
)
FIELD_LABELS = {
    "email": "Email", "first_name": "First name", "last_name": "Last name",
    "full_name": "Full name", "title": "Title", "company_name": "Company",
    "website": "Website", "industry": "Industry", "linkedin_url": "LinkedIn URL",
    "phone": "Phone", "personalization": "Personalization notes",
}
# Header spellings seen in exports from Apollo, Instantly, Smartlead, HubSpot,
# Sales Navigator tools and hand-made sheets. Compared after _header_key().
ALIASES = {
    "email": ("email", "e mail", "email address", "e mail address", "work email", "business email",
              "contact email", "email 1", "primary email", "mail"),
    "first_name": ("first name", "firstname", "first", "given name", "forename"),
    "last_name": ("last name", "lastname", "last", "surname", "family name"),
    "full_name": ("name", "full name", "contact name", "contact", "person"),
    "title": ("title", "job title", "position", "role", "job role", "designation"),
    "company_name": ("company", "company name", "organization", "organisation",
                     "organization name", "account", "account name", "business",
                     "business name", "employer"),
    "website": ("website", "company website", "domain", "company domain", "url",
                "website url", "web", "site", "homepage", "company url"),
    "industry": ("industry", "sector", "vertical", "company industry"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile", "person linkedin url",
                     "linkedin profile url", "profile url", "li url"),
    "phone": ("phone", "phone number", "mobile", "mobile phone", "telephone", "tel",
              "direct phone", "work phone", "cell"),
    "personalization": ("personalization", "personalisation", "personalization notes",
                        "notes", "note", "icebreaker", "custom note", "first line"),
}
# Columns that look like a verification verdict. They are read past, never
# trusted: an address is verified when Mercury verifies it.
STATUS_HEADERS = ("email status", "status", "verified", "email verified", "verification",
                  "verification status", "email verification", "deliverability")

# Consumer mailbox providers. Two people at gmail.com are not colleagues, so
# these domains never become a company.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
    "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "gmx.de", "mail.com", "zoho.com", "yandex.com", "yandex.ru", "fastmail.com",
    "hey.com", "tutanota.com", "comcast.net", "verizon.net", "att.net",
    "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net", "qq.com", "163.com",
    "126.com", "web.de", "t-online.de", "orange.fr", "free.fr", "laposte.net",
    "libero.it", "virgilio.it", "btinternet.com", "sky.com", "rediffmail.com",
    "hotmail.co.uk", "yahoo.co.uk", "outlook.es", "hotmail.es", "yahoo.es",
    "hotmail.fr", "yahoo.fr", "live.co.uk", "msn.cn", "naver.com", "daum.net",
})

EMAIL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


class ImportFileError(ControlError):
    """A file or request that cannot be previewed or committed.

    Codes: too_large, too_many_rows, not_utf8, empty, ambiguous_delimiter,
    bad_delimiter, missing_email_column, unknown_column, bad_mapping,
    bad_policy, invalid_rows, not_found. ``details`` carries what an
    interface needs to offer a fix (e.g. the delimiter candidates).
    """

    def __init__(self, code: str, message: str, **details):
        super().__init__(message, code, **details)


@dataclass
class ParsedFile:
    delimiter: str
    headers: list[str]
    # (row_number, {header: value}). Row 1 is the header, as in a spreadsheet.
    rows: list[tuple[int, dict[str, str]]]
    sha256: str


@dataclass
class CleanRow:
    row: int
    values: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Fields a contact should have before outreach, missing on this row.
    missing: list[str] = field(default_factory=list)
    # The company identity: a normalised domain, or a name key when there
    # is no usable domain. Empty when the row names no company at all.
    company_domain: str = ""
    company_key: str = ""


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ImportFileError(
            "not_utf8",
            "The file isn't UTF-8. Re-save it as \"CSV UTF-8\" (Excel) or "
            "File > Download > CSV (Google Sheets) and try again.",
        ) from error


def _records(text: str, delimiter: str, limit: int | None = None) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=False)
    out = []
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        out.append(record)
        if limit and len(out) >= limit:
            break
    return out


def detect_delimiter(text: str) -> str:
    """Comma, semicolon or tab, whichever splits the file into a steady
    table. Raises ``ambiguous_delimiter`` when more than one does."""
    fits = []
    for char in DELIMITERS:
        sample = _records(text, char, limit=25)
        if not sample:
            continue
        width = len(sample[0])
        if width > 1 and all(len(r) == width for r in sample):
            fits.append((width, char))
    if len(fits) == 1:
        return fits[0][1]
    if not fits:
        # One column, or ragged rows: comma is the honest default, and the
        # mapping step will say if the email column cannot be found.
        return ","
    raise ImportFileError(
        "ambiguous_delimiter",
        "This file splits cleanly on more than one delimiter. Choose "
        + " or ".join(DELIMITERS[c] for _, c in fits) + ".",
        candidates=[DELIMITERS[c] for _, c in fits],
    )


def read_csv(data: bytes, delimiter: str | None = None) -> ParsedFile:
    if len(data) > MAX_BYTES:
        raise ImportFileError(
            "too_large", f"The file is {len(data) / 1048576:.1f} MB. The limit is "
            f"{MAX_BYTES // 1048576} MB; split it and import the parts.")
    text = _decode(data)
    if not text.strip():
        raise ImportFileError("empty", "The file is empty.")
    if delimiter:
        char = DELIMITER_NAMES.get(delimiter, delimiter)
        if char not in DELIMITERS:
            raise ImportFileError("bad_delimiter", "Delimiter must be comma, semicolon or tab.")
    else:
        char = detect_delimiter(text)

    records = _records(text, char)
    if not records:
        raise ImportFileError("empty", "The file has no rows.")
    headers = [h.strip() for h in records[0]]
    seen: dict[str, int] = {}
    for i, header in enumerate(headers):
        # Blank or repeated headers still need a stable, distinct name.
        name = header or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        headers[i] = name
    body = records[1:]
    if len(body) > MAX_ROWS:
        raise ImportFileError(
            "too_many_rows", f"The file has {len(body):,} rows. The limit is "
            f"{MAX_ROWS:,} per import; split it and import the parts.")
    rows = []
    for index, record in enumerate(body, start=2):
        values = {h: (record[i].strip() if i < len(record) else "") for i, h in enumerate(headers)}
        if len(record) > len(headers) and any(c.strip() for c in record[len(headers):]):
            values["__extra__"] = "1"
        rows.append((index, values))
    return ParsedFile(char, headers, rows, hashlib.sha256(data).hexdigest())


def _header_key(header: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", header.lower()).split())


def auto_mapping(headers: list[str]) -> dict[str, str]:
    """{field: header} for every field whose header we recognise."""
    keys = {_header_key(h): h for h in reversed(headers)}
    mapping = {}
    for name in FIELDS:
        for alias in ALIASES[name]:
            if alias in keys:
                mapping[name] = keys[alias]
                break
    # "Name" next to a first/last pair is usually the company or a duplicate.
    if "full_name" in mapping and ("first_name" in mapping or "last_name" in mapping):
        del mapping["full_name"]
    return mapping


def resolve_mapping(headers: list[str], mapping: dict[str, str] | None) -> dict[str, str]:
    """The mapping to use: the caller's, else the automatic one. Validated."""
    if mapping is None:
        resolved = auto_mapping(headers)
    else:
        resolved = {}
        for name, header in mapping.items():
            if name not in FIELDS:
                raise ImportFileError(
                    "bad_mapping", f"Unknown field {name!r}. Fields: {', '.join(FIELDS)}.")
            if not header:
                continue
            if header not in headers:
                raise ImportFileError(
                    "unknown_column", f"No column named {header!r} in this file.",
                    headers=headers)
            resolved[name] = header
    if "email" not in resolved:
        raise ImportFileError(
            "missing_email_column",
            "Choose which column holds the email address. Every contact needs one.",
            headers=headers)
    return resolved


def ignored_status_columns(headers: list[str], mapping: dict[str, str]) -> list[str]:
    used = set(mapping.values())
    return [h for h in headers if h not in used and _header_key(h) in STATUS_HEADERS]


def normalize_email(raw: str) -> str:
    value = raw.strip().strip("<>").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value.strip().lower()


def valid_email(email: str) -> bool:
    if not email or len(email) > 254 or ".." in email:
        return False
    local = email.split("@", 1)[0]
    return bool(EMAIL_RE.match(email)) and 0 < len(local) <= 64 and not local.startswith(".") \
        and not local.endswith(".")


def linkedin_key(url: str) -> str:
    """One identity for every spelling of a LinkedIn URL: lowercase, no
    scheme, no www., no query, no trailing slash. Matches LINKEDIN_KEY_SQL
    exactly, so only ASCII is lowercased, as SQLite's lower() does."""
    value = "".join(c.lower() if c.isascii() else c for c in (url or "").strip())
    value = value.split("?", 1)[0]
    value = re.sub(r"^https?://", "", value)
    value = value[4:] if value.startswith("www.") else value
    return value.rstrip("/")


# The same normalisation in SQL, so rows stored in any spelling still match.
# Built from explicit prefix checks: SQLite's ltrim() strips a character set,
# which would mangle hosts like pt.linkedin.com.
def _sql_strip_prefix(expr: str, prefix: str) -> str:
    n = len(prefix)
    return (f"CASE WHEN substr({expr}, 1, {n}) = '{prefix}' "
            f"THEN substr({expr}, {n + 1}) ELSE {expr} END")


def _sql_linkedin_key() -> str:
    expr = "lower(linkedin_url)"
    expr = f"CASE WHEN instr({expr}, '?') > 0 THEN substr({expr}, 1, instr({expr}, '?') - 1) ELSE {expr} END"
    for prefix in ("https://", "http://", "www."):
        expr = _sql_strip_prefix(expr, prefix)
    return f"rtrim({expr}, '/')"


LINKEDIN_KEY_SQL = _sql_linkedin_key()


def _clean_linkedin(raw: str) -> str:
    key = linkedin_key(raw)
    if not key or "linkedin.com/" not in key:
        return ""
    return "https://www." + key


def clean_row(row: int, values: dict[str, str], mapping: dict[str, str]) -> CleanRow:
    out = CleanRow(row=row)
    raw = {name: " ".join((values.get(header) or "").split()) for name, header in mapping.items()}
    # Notes keep their line breaks.
    if "personalization" in mapping:
        raw["personalization"] = (values.get(mapping["personalization"]) or "").strip()
    if values.get("__extra__"):
        out.warnings.append("more cells than headers; the extra cells were ignored")

    for name, value in raw.items():
        limit = MAX_NOTES if name == "personalization" else MAX_FIELD
        if len(value) > limit:
            out.errors.append(f"{FIELD_LABELS[name]} is longer than {limit} characters")

    email = normalize_email(raw.get("email", ""))
    if not email:
        out.errors.append("no email address")
    elif not valid_email(email):
        out.errors.append("not a valid email address")
    out.values["email"] = email

    first, last = raw.get("first_name", ""), raw.get("last_name", "")
    if not (first or last) and raw.get("full_name"):
        parts = raw["full_name"].split(" ", 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else "")
    out.values["first_name"], out.values["last_name"] = first, last
    for name in ("title", "company_name", "industry"):
        out.values[name] = raw.get(name, "")

    if raw.get("linkedin_url"):
        out.values["linkedin_url"] = _clean_linkedin(raw["linkedin_url"])
        if not out.values["linkedin_url"]:
            out.warnings.append("LinkedIn URL isn't a linkedin.com link; ignored")
    else:
        out.values["linkedin_url"] = ""

    phone = raw.get("phone", "")
    if phone and len(re.sub(r"\D", "", phone)) < 7:
        out.warnings.append("phone number has too few digits; ignored")
        phone = ""
    out.values["phone"] = phone
    out.values["personalization"] = raw.get("personalization", "")

    domain = normalize_domain(raw.get("website", "")) if raw.get("website") else ""
    if domain and (is_junk(domain) or domain in FREE_MAIL):
        out.warnings.append(f"website {domain} is a directory, social or mail site; ignored")
        domain = ""
    out.values["website"] = domain
    if not domain and valid_email(email):
        mail_domain = email.split("@", 1)[1]
        if mail_domain not in FREE_MAIL:
            domain = mail_domain
    out.company_domain = domain
    if not domain and out.values["company_name"]:
        out.company_key = "import:" + _header_key(out.values["company_name"])

    out.missing = [label for label, value in (("first name", first), ("last name", last),
                                              ("title", out.values["title"])) if not value]
    return out


def fingerprint(sha256: str, *parts) -> str:
    """The identity of one commit: the file plus every choice made about it."""
    digest = hashlib.sha256(sha256.encode())
    for part in parts:
        digest.update(b"\x00" + repr(part).encode())
    return digest.hexdigest()
