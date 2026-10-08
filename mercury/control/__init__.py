"""Commands shared by every interface: dashboard, CLI and, later, MCP.

Transports parse requests and format results. Validation, lookups and error
codes live here so the same request behaves the same way everywhere.

Every service takes an OperatorContext (context.py: who is acting, through
which client, with which scopes) and raises ControlError subclasses
(errors.py: NotFound, Conflict, Invalid, Forbidden, ProhibitedField,
Unavailable), each with a stable ``code``.

  outbox     list, get, approve, reject, batch, edit, reschedule, regenerate
  sending    the global send switch: status, pause, resume
  discovery  provider menu, plan/estimate, background run, profile, stop
  runtime    the agent process: status, start, stop, log tail
  settings   the allowlisted mercury.yaml fields (secrets are refused)
  queries    the read-only views the dashboard serves
  personas, demos, imports   their own feature commands
"""
