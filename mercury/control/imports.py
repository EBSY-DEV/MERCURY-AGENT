"""CSV contact imports for the dashboard and the CLI.

Every interface calls the same four steps, so a file gives identical row
outcomes wherever it is imported:

  preview  read-only: parse, map, classify every row against the database
  commit   re-classify inside one write transaction, then insert or fill
  verify   explicit and separate: spend verifier credits on imported guesses
  release  explicit and separate: hand verified contacts to outreach

Imported contacts are held at status ``imported``, which the Writer, the
Scout re-verify sweep and the Sender all ignore. Nothing is verified, drafted
or sent because a file was imported. Failures raise ImportFileError with a
stable code.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter

import aiosqlite

from mercury.csv_import import (
    DELIMITERS, FIELD_LABELS, FIELDS, MAX_BYTES, MAX_ROWS, ImportFileError, auto_mapping,
    clean_row, fingerprint, ignored_status_columns, read_csv, resolve_mapping,
)
from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.state import _new_id, _utcnow

POLICIES = ("skip", "fill")
HELD_STATUS = "imported"
SOURCE = "csv_import"
# Row field -> prospects column, for the fill-missing policy. Email is filled
# only on a contact that has none; status, verification and score never are.
FILLABLE = {
    "first_name": "first_name", "last_name": "last_name", "title": "title",
    "phone": "phone", "linkedin_url": "linkedin_url", "company_name": "company",
    "industry": "industry", "personalization": "personalization_notes", "email": "email",
}
VERIFY_BATCH_LIMIT = 100


def decode_upload(content_b64: str) -> bytes:
    """Bytes from a dashboard upload, size-checked before decoding."""
    if len(content_b64) > (MAX_BYTES * 4) // 3 + 8:
        raise ImportFileError("too_large", f"The file is over the {MAX_BYTES // 1048576} MB limit.")
    try:
        return base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ImportFileError("empty", "The upload could not be read.") from error


def _limits() -> dict:
    return {"max_bytes": MAX_BYTES, "max_rows": MAX_ROWS}


class ImportService:
    def __init__(self, state, env=None):
        self.state, self.env = state, env

    async def ready(self):
        await self.state.init_db()
        return self

    # ── Classification ──

    @staticmethod
    async def _existing(db, values: dict) -> tuple[dict | None, str]:
        """The contact this row already is, and why we think so."""
        lookups = [("email", values["email"], "same email as an existing contact")]
        if values["linkedin_url"]:
            lookups.append(("linkedin_url", values["linkedin_url"],
                            "same LinkedIn profile as an existing contact"))
        for column, value, reason in lookups:
            async with db.execute(f"SELECT * FROM prospects WHERE {column} = ?", (value,)) as cur:
                row = await cur.fetchone()
            if row:
                return dict(row), reason
        first, last, company = values["first_name"], values["last_name"], values["company_name"]
        if first and last and company:
            async with db.execute(
                """SELECT * FROM prospects WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                   AND LOWER(company) = ?""", (first.lower(), last.lower(), company.lower()),
            ) as cur:
                row = await cur.fetchone()
            if row:
                return dict(row), "same name at the same company as an existing contact"
        return None, ""

    @staticmethod
    async def _fillable(db, existing: dict, values: dict) -> list[str]:
        fields = []
        for name, column in FILLABLE.items():
            if not values.get(name) or (existing.get(column) or "").strip():
                continue
            if name == "linkedin_url":
                async with db.execute("SELECT 1 FROM prospects WHERE linkedin_url = ?",
                                      (values[name],)) as cur:
                    if await cur.fetchone():
                        continue
            if name == "email":
                async with db.execute("SELECT 1 FROM prospects WHERE email = ?",
                                      (values[name],)) as cur:
                    if await cur.fetchone():
                        continue
            fields.append(name)
        return fields

    async def _plan(self, db, parsed, mapping, policy, exclude) -> list[dict]:
        """Every row's outcome and planned action. Reads only."""
        db.row_factory = aiosqlite.Row
        seen_email: dict[str, int] = {}
        seen_linkedin: dict[str, int] = {}
        plan = []
        for row_number, raw in parsed.rows:
            clean = clean_row(row_number, raw, mapping)
            v = clean.values
            item = {
                "row": row_number, "email": v["email"],
                "name": f"{v['first_name']} {v['last_name']}".strip(),
                "company": v["company_name"] or clean.company_domain,
                "title": v["title"], "errors": clean.errors, "warnings": clean.warnings,
                "missing": clean.missing, "fill": [], "existing_id": "", "suppressed": False,
                "values": v, "company_domain": clean.company_domain,
                "company_key": clean.company_key,
            }
            if clean.errors:
                item.update(outcome="invalid", action="exclude", reason="; ".join(clean.errors))
            elif v["email"] in seen_email:
                item.update(outcome="duplicate", action="skip",
                            reason=f"same email as row {seen_email[v['email']]}")
            elif v["linkedin_url"] and v["linkedin_url"] in seen_linkedin:
                item.update(outcome="duplicate", action="skip",
                            reason=f"same LinkedIn profile as row {seen_linkedin[v['linkedin_url']]}")
            else:
                existing, why = await self._existing(db, v)
                if existing:
                    item.update(outcome="duplicate", existing_id=existing["id"], reason=why)
                    if existing.get("status") == "opted_out":
                        item.update(action="skip", suppressed=True,
                                    reason=why + "; they opted out, and an import never reactivates them")
                    elif existing.get("email_status") == "invalid":
                        item.update(action="skip", suppressed=True,
                                    reason=why + "; the address is marked invalid")
                    elif policy == "fill":
                        item["fill"] = await self._fillable(db, existing, v)
                        item["action"] = "fill" if item["fill"] else "skip"
                        if not item["fill"]:
                            item["reason"] = why + "; nothing missing to fill"
                    else:
                        item["action"] = "skip"
                else:
                    item.update(outcome="incomplete" if clean.missing else "new", action="create",
                                reason=("needs " + ", ".join(clean.missing)) if clean.missing else "")
                seen_email.setdefault(v["email"], row_number)
                if v["linkedin_url"]:
                    seen_linkedin.setdefault(v["linkedin_url"], row_number)
            if row_number in exclude and item["action"] in ("create", "fill"):
                item.update(action="exclude", reason="left out by you")
            plan.append(item)
        return plan

    @staticmethod
    def _public(item: dict) -> dict:
        return {k: v for k, v in item.items() if k not in ("values", "company_domain", "company_key")}

    @staticmethod
    def _counts(plan: list[dict]) -> dict:
        outcomes = Counter(item["outcome"] for item in plan)
        actions = Counter(item["action"] for item in plan)
        return {
            "total": len(plan),
            **{k: outcomes.get(k, 0) for k in ("new", "incomplete", "duplicate", "invalid")},
            "suppressed": sum(1 for item in plan if item["suppressed"]),
            **{k: actions.get(k, 0) for k in ("create", "fill", "skip", "exclude")},
        }

    def _prepare(self, data: bytes, mapping, delimiter, policy):
        if policy not in POLICIES:
            raise ImportFileError("bad_policy", "Duplicate policy must be 'skip' or 'fill'.")
        parsed = read_csv(data, delimiter or None)
        return parsed, resolve_mapping(parsed.headers, mapping)

    # ── Preview ──

    async def preview(self, data: bytes, *, filename: str = "", mapping: dict | None = None,
                      delimiter: str = "", policy: str = "skip",
                      exclude_rows: list[int] | tuple = ()) -> dict:
        """What committing this file would do. Changes nothing, calls nothing."""
        parsed, resolved = self._prepare(data, mapping, delimiter, policy)
        async with self.state._connect() as db:
            await db.execute("PRAGMA query_only = ON")
            plan = await self._plan(db, parsed, resolved, policy, set(exclude_rows))
        return {
            "filename": filename, "delimiter": DELIMITERS[parsed.delimiter],
            "headers": parsed.headers, "mapping": resolved,
            "suggested_mapping": auto_mapping(parsed.headers),
            "fields": [{"key": k, "label": FIELD_LABELS[k]} for k in FIELDS],
            "ignored_columns": ignored_status_columns(parsed.headers, resolved),
            "policy": policy, "limits": _limits(), "counts": self._counts(plan),
            "rows": [self._public(item) for item in plan],
        }

    # ── Commit ──

    async def _company_id(self, db, item: dict, policy: str, cache: dict) -> str:
        v = item["values"]
        key = item["company_domain"] or item["company_key"]
        if not key:
            return ""
        if key in cache:
            return cache[key]
        column, value = (("domain", item["company_domain"]) if item["company_domain"]
                         else ("external_id", item["company_key"]))
        async with db.execute(f"SELECT id FROM companies WHERE {column} = ?", (value,)) as cur:
            row = await cur.fetchone()
        if row:
            company_id = row[0]
            if policy == "fill":
                await db.execute(
                    """UPDATE companies SET
                         name = CASE WHEN name = '' THEN ? ELSE name END,
                         industry = CASE WHEN industry = '' THEN ? ELSE industry END,
                         updated_at = ? WHERE id = ?""",
                    (v["company_name"], v["industry"], _utcnow().isoformat(), company_id))
        else:
            company_id = await self.state.insert_company(db, Company(
                name=v["company_name"] or item["company_domain"],
                domain=item["company_domain"],
                website=f"https://{v['website']}" if v["website"] else "",
                industry=v["industry"], source=SOURCE,
                external_id="" if item["company_domain"] else item["company_key"],
            ))
        cache[key] = company_id
        return company_id

    async def _apply(self, db, batch_id: str, item: dict, policy: str, cache: dict):
        v = item["values"]
        if item["action"] == "create":
            company_id = await self._company_id(db, item, policy, cache)
            prospect = Prospect(
                id=_new_id(), company_id=company_id, first_name=v["first_name"],
                last_name=v["last_name"], email=v["email"], email_status="guess",
                email_verified=False, phone=v["phone"], linkedin_url=v["linkedin_url"],
                title=v["title"], source=SOURCE, status=HELD_STATUS,
                personalization_notes=v["personalization"], company=v["company_name"],
                industry=v["industry"], import_batch_id=batch_id, import_row=item["row"],
            )
            new_id = prospect.id
            got = await self.state.insert_prospect(db, prospect)
            if got != new_id:
                item.update(action="skip", outcome="duplicate", existing_id=got,
                            reason="added by another process during the import")
                return
            item.update(action="created", prospect_id=got)
        elif item["action"] == "fill":
            sets, params = [], []
            for name in item["fill"]:
                column = FILLABLE[name]
                # Guarded per column, so a value typed in after the plan was
                # made is still never replaced.
                sets.append(f"{column} = CASE WHEN COALESCE({column}, '') = '' THEN ? ELSE {column} END")
                params.append(v[name])
                if name == "email":
                    sets.append("email_status = CASE WHEN COALESCE(email_status, '') = '' "
                                "THEN 'guess' ELSE email_status END")
            sets.append("updated_at = ?")
            params += [_utcnow().isoformat(), item["existing_id"]]
            await db.execute(f"UPDATE prospects SET {', '.join(sets)} WHERE id = ?", params)
            item.update(action="filled", prospect_id=item["existing_id"])
        elif item["action"] == "skip":
            item.update(action="skipped", prospect_id=item["existing_id"])
        else:
            item.update(action="excluded", prospect_id="")

    async def commit(self, data: bytes, *, filename: str = "", mapping: dict | None = None,
                     delimiter: str = "", policy: str = "skip",
                     exclude_rows: list[int] | tuple = (), skip_invalid: bool = False,
                     origin: str = "") -> dict:
        """Import the file in one transaction. Duplicates and suppression are
        re-checked under the write lock, because the database may have moved
        since the preview. Committing the same file with the same choices a
        second time returns the first result and changes nothing."""
        parsed, resolved = self._prepare(data, mapping, delimiter, policy)
        exclude = set(exclude_rows)
        fp = fingerprint(parsed.sha256, sorted(resolved.items()), parsed.delimiter, policy,
                         sorted(exclude), bool(skip_invalid))
        async with self.state._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT id FROM import_batches WHERE fingerprint = ?",
                                      (fp,)) as cur:
                    done = await cur.fetchone()
                if done:
                    await db.execute("ROLLBACK")
                    result = await self.batch(done[0])
                    result["already_committed"] = True
                    return result
                plan = await self._plan(db, parsed, resolved, policy, exclude)
                invalid = [i["row"] for i in plan if i["outcome"] == "invalid" and i["row"] not in exclude]
                if invalid and not skip_invalid:
                    raise ImportFileError(
                        "invalid_rows",
                        f"{len(invalid)} row(s) are invalid. Fix them, or leave them out "
                        "explicitly (skip invalid rows) and commit the rest.",
                        rows=invalid[:50])
                batch_id = _new_id()
                cache: dict[str, str] = {}
                for item in plan:
                    await self._apply(db, batch_id, item, policy, cache)
                actions = Counter(item["action"] for item in plan)
                await db.execute(
                    """INSERT INTO import_batches (id, fingerprint, filename, origin, policy,
                       total_rows, created, filled, skipped, excluded, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, fp, filename[:200], origin, policy, len(plan),
                     actions["created"], actions["filled"], actions["skipped"],
                     actions["excluded"], _utcnow().isoformat()))
                await db.executemany(
                    """INSERT INTO import_rows (batch_id, row_number, outcome, action, reason,
                       prospect_id) VALUES (?, ?, ?, ?, ?, ?)""",
                    [(batch_id, i["row"], i["outcome"], i["action"], i["reason"],
                      i.get("prospect_id", "")) for i in plan])
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.batch(batch_id, rows=False)
        result.update(already_committed=False, rows=[
            {**self._public(i), "prospect_id": i.get("prospect_id", "")} for i in plan])
        return result

    # ── Batches ──

    async def batches(self, limit: int = 20) -> list[dict]:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT b.*,
                     (SELECT COUNT(*) FROM prospects p WHERE p.import_batch_id = b.id
                        AND p.status = ?) AS held,
                     (SELECT COUNT(*) FROM prospects p WHERE p.import_batch_id = b.id
                        AND p.status = ? AND COALESCE(p.email_status, '') IN ('', 'guess'))
                        AS unverified
                   FROM import_batches b ORDER BY b.created_at DESC LIMIT ?""",
                (HELD_STATUS, HELD_STATUS, limit),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row.pop("fingerprint", None)
        return rows

    async def batch(self, batch_id: str, rows: bool = True) -> dict:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM import_batches WHERE id = ? OR id LIKE ? ORDER BY id LIMIT 2",
                (batch_id, f"{batch_id}%"),
            ) as cur:
                found = [dict(r) for r in await cur.fetchall()]
            if len(found) != 1:
                raise ImportFileError("not_found", f"No single import matches {batch_id!r}.")
            batch = found[0]
            batch.pop("fingerprint", None)
            async with db.execute(
                """SELECT email_status, status, COUNT(*) AS n FROM prospects
                   WHERE import_batch_id = ? GROUP BY email_status, status""", (batch["id"],),
            ) as cur:
                contacts = [dict(r) for r in await cur.fetchall()]
            out = {"batch": batch, "contacts": contacts}
            if rows:
                async with db.execute(
                    """SELECT row_number AS row, outcome, action, reason, prospect_id
                       FROM import_rows WHERE batch_id = ? ORDER BY row_number""", (batch["id"],),
                ) as cur:
                    out["rows"] = [dict(r) for r in await cur.fetchall()]
        return out

    # ── Verification (explicit, separate, costs credits) ──

    def _providers(self) -> list[dict]:
        keys = [("Reoon", "reoon_api_key", "600 free / month"),
                ("ZeroBounce", "zerobounce_api_key", "100 free / month"),
                ("Hunter", "hunter_api_key", "50 free / month")]
        return [{"name": name, "free_tier": tier} for name, attr, tier in keys
                if getattr(self.env, attr, "")]

    async def _unverified(self, batch_id: str, after_row: int, limit: int | None) -> list[dict]:
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            sql = ("""SELECT id, email, import_row FROM prospects
                      WHERE import_batch_id = ? AND status = ? AND email != ''
                        AND COALESCE(email_status, '') IN ('', 'guess') AND import_row > ?
                      ORDER BY import_row""" + (" LIMIT ?" if limit else ""))
            params = (batch_id, HELD_STATUS, after_row) + ((limit,) if limit else ())
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def verify_estimate(self, batch_id: str) -> dict:
        """What verifying this batch would spend. No provider is called."""
        batch = (await self.batch(batch_id, rows=False))["batch"]
        pending = len(await self._unverified(batch["id"], 0, None))
        providers = self._providers()
        return {
            "batch_id": batch["id"], "addresses": pending, "providers": providers,
            "cost": (f"Up to {pending} verification credit(s), one per address, from the first "
                     "configured provider that gives an answer." if providers else
                     "No verification provider is configured. Add REOON_API_KEY, "
                     "ZEROBOUNCE_API_KEY or HUNTER_API_KEY to .env."),
        }

    async def _verify_one(self, email: str) -> str:
        from mercury.integrations.email_finder import _verify_candidate, classify_mx, get_mx_host
        mx = await get_mx_host(email.split("@", 1)[1])
        if not mx:
            return "invalid"
        status, _ = await _verify_candidate(email, classify_mx(mx), self.env)
        return status or "guess"

    async def verify(self, batch_id: str, *, limit: int = 25, after_row: int = 0,
                     progress=None) -> dict:
        """Verify up to ``limit`` unverified addresses in a batch, in row
        order after ``after_row``. Pass back ``next_after_row`` to continue.
        Contacts that opted out, bounced or already verified are never sent
        to a provider."""
        from mercury.integrations.email_finder import VerifierExhausted
        if not self._providers():
            raise ImportFileError("no_verifier", (await self.verify_estimate(batch_id))["cost"])
        batch = (await self.batch(batch_id, rows=False))["batch"]
        limit = max(1, min(int(limit), VERIFY_BATCH_LIMIT))
        todo = await self._unverified(batch["id"], after_row, limit)
        results: Counter = Counter()
        stopped, last_row = "", after_row
        for index, row in enumerate(todo, start=1):
            try:
                status = await self._verify_one(row["email"])
            except VerifierExhausted as error:
                stopped = f"Verifier out of credits: {error}"
                break
            await self.state.update_prospect_email(row["id"], row["email"], status)
            results[status] += 1
            last_row = row["import_row"]
            if progress:
                await progress(index, len(todo))
        remaining = len(await self._unverified(batch["id"], last_row, None))
        return {"batch_id": batch["id"], "checked": sum(results.values()),
                "results": dict(results), "next_after_row": last_row,
                "remaining": 0 if stopped else remaining, "stopped": stopped}

    # ── Release to outreach (explicit, separate) ──

    async def release(self, batch_id: str, include_risky: bool = False) -> dict:
        """Hand a batch's deliverable contacts to the normal pipeline, where
        the Writer drafts for them (and drafts wait in the Outbox for approval
        unless approval is off). Unverified and invalid contacts stay held."""
        batch = (await self.batch(batch_id, rows=False))["batch"]
        statuses = ("verified", "risky") if include_risky else ("verified",)
        marks = ",".join("?" for _ in statuses)
        async with self.state._connect() as db:
            cursor = await db.execute(
                f"""UPDATE prospects SET status = 'new', updated_at = ?
                    WHERE import_batch_id = ? AND status = ? AND email_status IN ({marks})""",
                (_utcnow().isoformat(), batch["id"], HELD_STATUS, *statuses))
            released = cursor.rowcount
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE import_batch_id = ? AND status = ?",
                (batch["id"], HELD_STATUS)) as cur:
                (held,) = await cur.fetchone()
        return {"batch_id": batch["id"], "released": released, "still_held": held}
