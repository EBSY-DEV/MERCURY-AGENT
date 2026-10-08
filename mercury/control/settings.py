"""Supported configuration: the mercury.yaml fields an operator may change
from the dashboard, the CLI or MCP.

Only the fields in EDITABLE can change here. Anything else, a secret above
all, is refused with ProhibitedField before either file is read for writing.
Secrets live in .env and are set from the dashboard's Settings tab or by
hand; mailboxes have their own validated editor (mercury/inbox_settings.py).
A change is validated against the whole config before it is written, and
the write is atomic and keeps the file's comments and key order.
"""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path

from pydantic import ValidationError

from mercury.control.errors import Invalid, ProhibitedField

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
SECRET_HINTS = ("password", "secret", "token", "api_key", "apikey", "credential", "private_key", "_env")


def looks_secret(path: str) -> bool:
    key = str(path).lower()
    return any(hint in key for hint in SECRET_HINTS)


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
    def __init__(self, ctx, source: Path | None = None, target: Path | None = None):
        # Defaults: the config Mercury reads, written to mercury.local.yaml
        # when that is the tracked template (see config_paths).
        if source is None:
            source, private = config_paths()
            target = target or private
        self.ctx, self.source, self.target = ctx, Path(source), Path(target or source)

    def _load(self):
        from mercury.config import MercuryConfig

        import yaml
        return MercuryConfig(**(yaml.safe_load(self.source.read_text()) or {}))

    async def get(self) -> dict:
        """Every editable field and its current value."""
        self.ctx.require("read")
        config = self._load()
        return {"fields": {path: _lookup(config, path) for path in EDITABLE},
                "config_file": str(self.source)}

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

    async def update(self, changes: dict) -> dict:
        """Apply allowlisted changes. Everything is checked (permissions, the
        allowlist, types, then the whole resulting config) before the file
        is written, so a refused change leaves it untouched."""
        self.ctx.require("edit")
        changes = self.validate(changes)

        from ruamel.yaml import YAML

        from mercury.config import MercuryConfig
        from mercury.inbox_settings import _plain, atomic_write

        rt_yaml = YAML(typ="rt")
        rt_yaml.preserve_quotes = True
        rt_yaml.width = 4096
        raw = rt_yaml.load(self.source.read_text()) or {}
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
        return {"changed": changes, "config_file": str(self.target),
                "fields": {path: _lookup(validated, path) for path in EDITABLE}}
