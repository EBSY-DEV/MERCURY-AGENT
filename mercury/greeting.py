"""How a draft addresses its reader: by name, by role, or not at all.

The Writer asks ``plan_greeting`` once per contact. The answer comes from the
registry resolver (mercury/registry/resolver.py), which only returns a first
name when there is evidence for it:

* ``named`` (the contact's own record) or ``registry`` (an accepted registry
  name): greet by that first name.
* anything else at a shared inbox (info@, office@): there is nobody to greet.
  No generic greeting ("Hi there", "Hello team"); the email instead makes a
  short routing request, addressed to the role the campaign brief names
  (``offers[].routing_role``), else ``writer.routing_role``.
* a registry name still waiting for a person's accept (``registry_pending``)
  is NOT used. It is treated like no name.
* a personal address with no usable name: no greeting either, no routing
  request (the address is not a shared one).

``prompt_lines`` turns the plan into the prompt text; the deterministic
backstop for a model that writes a generic greeting anyway is
``draft_rules.strip_generic_greeting``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mercury.registry.resolver import is_shared_inbox, resolve_contact_name

NAMED, ROUTING, NONE = "name", "routing", "none"


@dataclass
class GreetingPlan:
    mode: str = NONE
    first_name: str = ""
    full_name: str = ""
    role: str = ""
    # The resolver's status, for the prompt's fact line and for tests.
    status: str = ""
    shared_inbox: bool = False

    @property
    def greets_by_name(self) -> bool:
        return self.mode == NAMED


def routing_role(config, brief=None) -> str:
    """The role a shared inbox is asked to pass the message to: the selected
    offer's, else the writer's configured fallback."""
    if brief is not None and brief.routing_role:
        return brief.routing_role
    fallback = getattr(getattr(config, "writer", None), "routing_role", "") or ""
    return fallback.strip() or "the person who handles this"


async def plan_greeting(state, config, prospect, company=None, brief=None) -> GreetingPlan:
    """The greeting decision for one contact. Never fetches: it reads the
    cached registry result and review through ``resolve_contact_name``."""
    found = await resolve_contact_name(state, prospect, company)
    shared = is_shared_inbox(getattr(prospect, "email", "") or "")
    if found["status"] in ("named", "registry") and found["first_name"]:
        return GreetingPlan(NAMED, found["first_name"], found["full_name"], status=found["status"],
                            shared_inbox=shared)
    if shared:
        return GreetingPlan(ROUTING, role=routing_role(config, brief), status=found["status"],
                            shared_inbox=True)
    return GreetingPlan(NONE, status=found["status"])


async def preview_greeting(state, config, prospect, company=None, brief=None) -> dict:
    """How the first email would open, for the dashboard: the line it opens
    with when a name is used, and the routing request it makes without one.
    Both are examples of the shape; the Writer words each email itself.

    ``mode`` is what would happen now (``name``, ``routing`` or ``none``).
    ``with_name`` is also filled for a registry name still waiting for a
    person's accept, so the two outcomes can be compared; it is empty when
    there is no name to greet."""
    plan = await plan_greeting(state, config, prospect, company, brief)
    first = plan.first_name
    if not first:
        found = await resolve_contact_name(state, prospect, company)
        first = (found.get("suggestion") or {}).get("first_name", "")
    role = plan.role or routing_role(config, brief)
    from_offer = bool(brief is not None and brief.routing_role)
    return {
        "mode": plan.mode, "status": plan.status, "shared_inbox": plan.shared_inbox,
        "first_name": first,
        "with_name": f"Hi {first}," if first else "",
        "without_name": f"Could you pass this to {role}?" if plan.shared_inbox else "",
        "role": role, "role_source": "offer" if from_offer else "writer",
    }


def business_names(prospect, company=None) -> list[str]:
    """Names a greeting must not address: the business itself."""
    return [n for n in (getattr(company, "name", ""), getattr(prospect, "company", "")) if n]


def fact_line(plan: GreetingPlan, prospect) -> str:
    """The Name line of the FACTS block."""
    if plan.mode == NAMED:
        suffix = " (name from a public business registry)" if plan.status == "registry" else ""
        return f"- Name: {plan.full_name or plan.first_name}{suffix}. First name to greet them by: {plan.first_name}"
    local = (getattr(prospect, "email", "") or "").split("@", 1)[0]
    if plan.mode == ROUTING:
        return f"- Contact: a shared business inbox ({local}@), no person's name is known"
    return "- Contact: no usable name is known"


def prompt_lines(plan: GreetingPlan, step: int = 1) -> str:
    """The greeting requirement for one email of a thread."""
    if plan.mode == NAMED:
        return (f"Greeting: open by greeting them with their first name only ({plan.first_name}), the way "
                "a person writes it in the email's language. Never their last name, never the business name.")
    generic = ('No generic greeting in any language ("Hi there", "Hello team", "Hello", "Hola equipo", '
               '"Dear sir or madam") and no greeting that names the business: start with the first sentence.')
    if plan.mode == ROUTING:
        if step <= 1:
            return (f"Greeting: this goes to a shared inbox and no person's name is known. {generic} "
                    f"Make one short routing request addressed to {plan.role}: ask who that is, or to pass "
                    "the message on to them. That request is the email's one ask and takes the place of "
                    "the call to action.")
        return (f"Greeting: still a shared inbox with no known name. {generic} Keep writing to {plan.role}. "
                "Use the call to action as given; do not repeat the routing request word for word.")
    return f"Greeting: no name is known for this reader. {generic}"


def sequence_lines(plans: list[GreetingPlan]) -> str:
    """The greeting requirement for a template shared by a whole group."""
    if plans and all(p.mode == ROUTING for p in plans):
        role = plans[0].role if len({p.role for p in plans}) == 1 else "the right person"
        return (f"Greeting: none of these readers has a known name (shared inboxes). No greeting line and no "
                f"generic greeting in any language; open each email with its first sentence and write to {role}.")
    return ("Greeting: if you write one, put it alone on the first line and build it from {{first_name}} "
            "(for example a salutation plus the merge variable). Mercury removes that line for a reader "
            "with no known name, so every email must also read well without it. Never a generic greeting "
            '("Hi there", "Hello team").')
