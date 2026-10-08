"""Supported configuration: the mercury.yaml fields an operator may change
from the dashboard, the CLI or MCP.

Only the fields in EDITABLE can change here. Anything else, a secret above
all, is refused with ProhibitedField before either file is read for writing.
Secrets live in .env and are set from the dashboard's Settings tab or by
hand; mailboxes have their own validated editor (mercury/inbox_settings.py).
A change is validated against the whole config before it is written, and
the write is atomic and keeps the file's comments and key order.

The config's revision is a hash of the file Mercury reads. A change names
the revision it was decided on and fails with stale_revision when the file
changed since (another client, or a hand edit). Changes are audited and
may carry a request key for idempotent replay (control/audit.py).
"""

from __future__ import annotations

import hashlib
import os
from io import StringIO
from pathlib import Path

from pydantic import ValidationError

from mercury.control.audit import looks_secret, redact, run_command
from mercury.control.errors import Conflict, Invalid, ProhibitedField

# Dotted path -> type. bool is checked before int: True is an int in Python.
EDITABLE: dict[str, type] = {
    "channels.email.require_approval": bool,
    "channels.email.auto_approve_followups": bool,
    "channels.email.send_to_risky": bool,
    "channels.email.spread_sends": bool,
    "channels.email.max_daily_sends": int,
    "channels.email.ooo_resume_buffer_days": int,
    "usage.max_daily_claude_percent": float,
    "usage.heartbeat_interval_minutes": int,
    "usage.quiet_hours.start": str,
    "usage.quiet_hours.end": str,
    "usage.quiet_hours.timezone": str,
}


def config_revision(text: str) -> str:
    """The revision of a config file: a short hash of its content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def config_paths() -> tuple[Path, Path | None]:
    """(the config Mercury reads, where a change is written). A change to the
    tracked mercury.yaml template goes to the gitignored mercury.local.yaml,
    which wins from then on; an explicit MERCURY_CONFIG is written in place."""
    from mercury.config import _find_config_file

    source = Path(_find_config_file())
    private = (source.with_name("mercury.local.yaml")
               if source.name == "mercury.yaml" and not os.getenv("MERCURY_CONFIG") else None)
    return source, private


def mail_context(env_file: Path):
    """(config, pool) built exactly as the sender builds them. Re-reads .env
    on each call, so a password added by hand shows up without a restart."""
    from dotenv import dotenv_values

    from mercury.config import load_config, load_env
    from mercury.integrations.mailboxes import MailboxPool

    # Read .env over a copy of the environment; never mutate os.environ
    # here (an agent started from this process inherits it).
    values = dict(os.environ)
    if Path(env_file).exists():
        values.update({k: v for k, v in dotenv_values(str(env_file), interpolate=False).items()
                       if v is not None})
    config = load_config()
    return config, MailboxPool.from_config(config, load_env(values))


def _lookup(data, path: str):
    for part in path.split("."):
        data = getattr(data, part)
    return data


def _check_value(path: str, value):
    kind = EDITABLE[path]
    if kind is bool:
        ok = isinstance(value, bool)
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, str) and not any(c in value for c in "\r\n\x00")
        value = value.strip() if ok else value
    if not ok:
        label = {bool: "true or false", int: "a whole number", float: "a number", str: "one line of text"}[kind]
        raise Invalid(f"{path} must be {label}", code="invalid_value", field=path)
    return value


class ConfigService:
    def __init__(self, ctx, state, source: Path | None = None, target: Path | None = None):
        # state: where changes are audited and request keys recorded.
        # Paths default to the config Mercury reads, written to
        # mercury.local.yaml when that is the tracked template (see config_paths).
        if source is None:
            source, private = config_paths()
            target = target or private
        self.ctx, self.state = ctx, state
        self.source, self.target = Path(source), Path(target or source)

    def _load(self):
        from mercury.config import MercuryConfig

        import yaml
        return MercuryConfig(**(yaml.safe_load(self.source.read_text()) or {}))

    async def get(self) -> dict:
        """Every editable field and its current value."""
        self.ctx.require("read")
        text = self.source.read_text()
        config = self._load()
        return {"fields": {path: _lookup(config, path) for path in EDITABLE},
                "config_file": str(self.source), "revision": config_revision(text)}

    def validate(self, changes) -> dict:
        """The changes, type-checked, or the first reason they can't apply.
        Reads nothing and writes nothing."""
        if not isinstance(changes, dict) or not changes:
            raise Invalid("give at least one field to change, as {\"dotted.path\": value}")
        refused = [str(path) for path in changes if path not in EDITABLE]
        if refused:
            secret = [path for path in refused if looks_secret(path)]
            if secret:
                raise ProhibitedField(
                    f"{', '.join(sorted(secret))}: secrets cannot be changed here. Set them in .env "
                    "(dashboard Settings tab).", secret, code="secret_field")
            raise ProhibitedField(
                f"{', '.join(sorted(refused))}: not a supported setting. Editable: "
                f"{', '.join(EDITABLE)}", refused, code="unknown_field")
        return {path: _check_value(path, value) for path, value in changes.items()}

    async def update(self, changes: dict, expected_revision: str | None = None) -> dict:
        """Apply allowlisted changes to the revision the caller read.
        Everything is checked (permissions, the allowlist, types, the
        revision, then the whole resulting config) before the file is
        written, so a refused change leaves it untouched."""
        async def work(trail):
            return await self._update(changes, expected_revision, trail)
        return await run_command(
            self.state, self.ctx, "config.update", scope="edit",
            params={"changes": changes, "revision": expected_revision}, work=work,
            object_type="config", object_id=self.target.name, revision_before=expected_revision)

    async def _update(self, changes, expected_revision, trail) -> dict:
        changes = self.validate(changes)
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise Invalid("give the config revision you read (revision)", code="revision_required")

        from ruamel.yaml import YAML

        from mercury.config import MercuryConfig
        from mercury.inbox_settings import _plain, atomic_write

        text = self.source.read_text()
        current = config_revision(text)
        if expected_revision.strip() != current:
            raise Conflict("the configuration changed since you read it; reload it and try again",
                           code="stale_revision", revision=current)
        rt_yaml = YAML(typ="rt")
        rt_yaml.preserve_quotes = True
        rt_yaml.width = 4096
        raw = rt_yaml.load(text) or {}
        for path, value in changes.items():
            node = raw
            *parents, leaf = path.split(".")
            for part in parents:
                # A bare `usage:` key loads as None; treat it as empty.
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[leaf] = value
        try:
            validated = MercuryConfig(**_plain(raw))
        except ValidationError as error:
            first = error.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "config"
            raise Invalid(f"{field}: {first['msg']}", code="invalid_value", field=field) from None
        out = StringIO()
        rt_yaml.dump(raw, out)
        atomic_write(self.target, out.getvalue())
        revision = config_revision(out.getvalue())
        trail.record(self.target.name, current, revision, changes=redact(changes))
        return {"changed": changes, "config_file": str(self.target), "revision": revision,
                "fields": {path: _lookup(validated, path) for path in EDITABLE}}
