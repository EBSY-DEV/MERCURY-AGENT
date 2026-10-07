"""Private inbox configuration used by the dashboard. Never return passwords."""

import os
import re
import tempfile
import uuid
from io import StringIO
from pathlib import Path

import yaml
from dotenv import dotenv_values
from ruamel.yaml import YAML
from dotenv.parser import parse_stream
from pydantic import ValidationError

from mercury.config import MailboxConfig, MercuryConfig, load_env
from mercury.integrations.mailboxes import local_today
from mercury.integrations.smtp_mail import SmtpImapProvider


def _plain(node):
    """Convert ruamel's round-trip containers into plain dicts/lists."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def atomic_write(path: Path, content: str):
    # Resolve an existing symlink so private state-repo configurations stay linked.
    path = path.resolve()
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_env(path: Path, updates: dict[str, str]):
    """Preserve comments and unrelated keys; quote literal credential values."""
    path = path.resolve()
    original = path.read_text() if path.exists() else ""
    replacements = {}
    for key, value in updates.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        replacements[key] = f"{key}='{escaped}'\n"
    seen = set()
    lines = []
    for binding in parse_stream(StringIO(original)):
        if binding.key in replacements:
            # Replace the first occurrence only; drop later duplicates so a
            # stale copy can never shadow the rotated value.
            if binding.key not in seen:
                lines.append(replacements[binding.key])
                seen.add(binding.key)
        else:
            lines.append(binding.original.string)
    content = "".join(lines)
    missing = [line for key, line in replacements.items() if key not in seen]
    if missing and content and not content.endswith("\n"):
        content += "\n"
    atomic_write(path, content + "".join(missing))


def env_values(path: Path) -> dict[str, str]:
    values = dict(os.environ)
    if path.exists():
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items()
                       if v is not None})
    return values


def configured_inboxes(config: MercuryConfig, env) -> list[MailboxConfig]:
    boxes = list(config.channels.email.mailboxes)
    if not boxes and config.channels.email.provider == "smtp" and env.smtp_username:
        # Materialize the old single inbox before adding rotation. Otherwise
        # pre-rotation threads could be assigned to the newly added address.
        boxes.append(MailboxConfig(
            email=config.persona.email or env.smtp_username,
            username=env.smtp_username, password_env="SMTP_PASSWORD",
            imap_username=env.imap_username,
            imap_password_env="IMAP_PASSWORD" if env.imap_password else "",
            daily_cap=config.channels.email.max_daily_sends,
        ))
    return boxes


def public_inbox(box: MailboxConfig, config: MercuryConfig, env) -> dict:
    provider = SmtpImapProvider(config, env, mailbox=box)
    data = box.model_dump(mode="json", exclude={"password_env", "imap_password_env"})
    data.update(password_set=bool(env.secret(box.password_env)),
                imap_password_set=bool(box.imap_password_env and env.secret(box.imap_password_env)),
                configured=provider.is_configured())
    return data


def settings(config_path: Path, env_path: Path) -> dict:
    config = MercuryConfig(**yaml.safe_load(config_path.read_text()))
    env = load_env(env_values(env_path))
    return {
        "provider": config.channels.email.provider,
        "max_daily_sends": config.channels.email.max_daily_sends,
        "today": local_today(config).isoformat(),
        "defaults": {"smtp_host": env.smtp_host, "smtp_port": env.smtp_port,
                     "imap_host": env.imap_host or env.smtp_host, "imap_port": env.imap_port},
        "inboxes": [public_inbox(box, config, env) for box in configured_inboxes(config, env)],
    }


class InboxError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def save(config_path: Path, env_path: Path, data: dict, email: str | None = None,
         private_path: Path | None = None) -> dict:
    allowed = {"email", "name", "username", "password", "smtp_host", "smtp_port",
               "imap_host", "imap_port", "daily_cap", "warmup_start", "enabled",
               "activate_smtp", "imap_username", "imap_password"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise InboxError("Use only the supported inbox settings.")
    # Round-trip loader so saving keeps the user's comments and key order.
    rt_yaml = YAML(typ="rt")
    rt_yaml.preserve_quotes = True
    rt_yaml.width = 4096
    raw = rt_yaml.load(config_path.read_text()) or {}
    # A bare `channels:` / `email:` key loads as None; treat it as empty.
    if not isinstance(raw.get("channels"), dict):
        raw["channels"] = {}
    if not isinstance(raw["channels"].get("email"), dict):
        raw["channels"]["email"] = {}
    config = MercuryConfig(**_plain(raw))
    env = load_env(env_values(env_path))
    boxes = configured_inboxes(config, env)
    current = next((b for b in boxes if b.email == (email or "").lower()), None)
    if email and current is None:
        raise InboxError("This inbox is no longer configured. Refresh and try again.", 404)
    if config.channels.email.provider != "smtp" and data.get("activate_smtp") is not True:
        raise InboxError("Confirm switching the email provider to SMTP + IMAP to use these inboxes.")
    if "activate_smtp" in data and not isinstance(data["activate_smtp"], bool):
        raise InboxError("The provider confirmation must be true or false.")
    changes = {k: v for k, v in data.items() if k not in ("password", "imap_password", "activate_smtp")}
    for field in ("email", "name", "username", "smtp_host", "imap_host", "imap_username"):
        if field in changes and not isinstance(changes[field], str):
            raise InboxError(f"{field.replace('_', ' ')} must be text.")
    if any(isinstance(v, str) and ("\n" in v or "\r" in v) for v in changes.values()):
        raise InboxError("Inbox settings must not contain line breaks.")
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise InboxError("New outreach must be enabled or disabled.")
    for field in ("smtp_port", "imap_port", "daily_cap"):
        if field in changes and (isinstance(changes[field], bool) or not isinstance(changes[field], int)):
            raise InboxError(f"{field.replace('_', ' ')} must be a whole number.")
    for field in ("smtp_port", "imap_port"):
        if field in changes and not 0 <= changes[field] <= 65535:
            raise InboxError("Ports must be between 1 and 65535, or blank to use the default.")
    if current:
        if "email" in changes and changes["email"].strip().lower() != current.email:
            raise InboxError("An inbox address cannot be changed because existing threads use it.")
        values = current.model_dump(mode="json") | changes
    else:
        values = {"warmup_start": local_today(config).isoformat(), "enabled": True} | changes
        values["password_env"] = f"MAILBOX_{uuid.uuid4().hex.upper()}_PASSWORD"
    if values.get("warmup_start") == "":
        values["warmup_start"] = None
    try:
        box = MailboxConfig(**values)
    except (ValidationError, TypeError):
        raise InboxError("Check the email address, daily limit, and warm-up date.") from None
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", box.email):
        raise InboxError("Enter a valid inbox email address.")
    if not current and any(b.email == box.email for b in boxes):
        raise InboxError("This inbox already exists. Edit it instead.", 409)
    password = data.get("password")
    imap_password = data.get("imap_password")
    for label, value in (("password", password), ("IMAP password", imap_password)):
        if value is not None and not isinstance(value, str):
            raise InboxError(f"The {label} must be text.")
        if value and any(c in value for c in ("\n", "\r", "\x00")):
            raise InboxError(f"The {label} must not contain line breaks or null characters.")
    updates = {}
    if password:
        # Editing a shared legacy credential must not change other inboxes.
        shared = sum(b.password_env == box.password_env for b in boxes) > 1
        if shared or not box.password_env.startswith("MAILBOX_"):
            box.password_env = f"MAILBOX_{uuid.uuid4().hex.upper()}_PASSWORD"
        updates[box.password_env] = password
        legacy_imap = box.imap_password_env and not box.imap_password_env.endswith("_IMAP_PASSWORD")
        if legacy_imap and box.imap_password_env != box.password_env and not imap_password:
            # A legacy inbox materialised from SMTP_PASSWORD + IMAP_PASSWORD
            # keeps a separate IMAP secret. Rotating only the SMTP password
            # would leave IMAP on the stale one, so unless a new IMAP password
            # is supplied, assume the common case: one credential for both.
            # A MAILBOX_*_IMAP_PASSWORD key set from the dashboard is kept.
            box.imap_password_env = box.password_env
    if imap_password:
        # A distinct IMAP secret gets its own MAILBOX_* key; it is never
        # written into YAML and never echoed back by public_inbox.
        if not (box.imap_password_env.startswith("MAILBOX_")
                and box.imap_password_env.endswith("_IMAP_PASSWORD")
                and sum(b.imap_password_env == box.imap_password_env for b in boxes) <= 1):
            box.imap_password_env = f"MAILBOX_{uuid.uuid4().hex.upper()}_IMAP_PASSWORD"
        updates[box.imap_password_env] = imap_password
    if current:
        boxes = [box if b.email == current.email else b for b in boxes]
    else:
        boxes.append(box)
    raw["channels"]["email"]["provider"] = "smtp"
    raw["channels"]["email"]["mailboxes"] = [b.model_dump(mode="json") for b in boxes]
    # Validate the entire result before either file is changed.
    validated = MercuryConfig(**_plain(raw))
    target = private_path or config_path
    target = target.resolve()
    before = target.read_text() if target.exists() else None
    out = StringIO()
    rt_yaml.dump(raw, out)
    atomic_write(target, out.getvalue())
    try:
        if updates:
            write_env(env_path, updates)
    except OSError:
        if before is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, before)
        raise
    os.environ.update(updates)
    env = load_env(env_values(env_path))
    return {"success": True, "inbox": public_inbox(box, validated, env)}
