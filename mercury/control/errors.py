"""Domain errors shared by every command service.

A service raises one of these and never builds a transport response. Each
interface translates the class (or the stable ``code``) into its own shape:
an HTTP status in the dashboard, an exit message in the CLI, a tool error in
MCP. ``details`` carries what an interface needs to offer a fix, and never a
secret value.
"""

from __future__ import annotations


class ControlError(ValueError):
    """A command that cannot proceed. ``code`` is stable across interfaces."""

    default_code = "invalid"

    def __init__(self, message: str, code: str = "", **details):
        super().__init__(message)
        self.code = code or self.default_code
        self.details = details


class NotFound(ControlError):
    """The object named does not exist (or no longer does)."""
    default_code = "not_found"


class Conflict(ControlError):
    """The object exists but is not in a state that allows this change, or
    another change got there first."""
    default_code = "conflict"


class Invalid(ControlError):
    """The request itself is malformed or out of range."""
    default_code = "invalid"


class Forbidden(ControlError):
    """The operator may not do this through this interface."""
    default_code = "forbidden"


class ProhibitedField(Forbidden):
    """A change names a field outside the editable allowlist: a secret, or a
    field this interface does not support. Raised before anything is written."""
    default_code = "prohibited_field"

    def __init__(self, message: str, fields: list[str], code: str = ""):
        super().__init__(message, code, fields=sorted(fields))
        self.fields = sorted(fields)


class Unavailable(ControlError):
    """A dependency the command needs failed or is not set up (config, a
    provider, the writer)."""
    default_code = "unavailable"
