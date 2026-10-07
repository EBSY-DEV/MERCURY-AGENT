"""Who is acting, and through which interface.

Every command service takes an OperatorContext. Today every caller is local
(the dashboard on 127.0.0.1, the CLI, a stdio MCP server) and holds every
scope. A remote MCP client will carry its OAuth subject and only the scopes
it was granted; the services already check them, so nothing changes for
callers when that lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mercury.control.errors import Forbidden

CLIENTS = ("dashboard", "cli", "mcp")
# read: queries; edit: drafts and supported config; approve: outbox review;
# run: sending controls, discovery, the agent process; admin: everything.
SCOPES = frozenset({"read", "edit", "approve", "run", "admin"})


@dataclass(frozen=True)
class OperatorContext:
    client: str
    operator: str = "local"
    scopes: frozenset[str] = field(default=SCOPES)
    # Optional caller-chosen key for the request. Unused until idempotent
    # replays exist; carried so every interface already passes one through.
    request_id: str = ""

    def __post_init__(self):
        if self.client not in CLIENTS:
            raise ValueError(f"client must be one of {', '.join(CLIENTS)}")
        unknown = set(self.scopes) - SCOPES
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(sorted(unknown))}")

    @classmethod
    def local(cls, client: str, request_id: str = "") -> "OperatorContext":
        """The person at this machine: every scope."""
        return cls(client=client, request_id=request_id)

    @property
    def actor(self) -> str:
        """What the activity log records as the agent behind an action."""
        return self.client

    def require(self, scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        if scope not in self.scopes and "admin" not in self.scopes:
            raise Forbidden(f"This needs the {scope} permission.", code="missing_scope", scope=scope)
