"""Who a memory involves: people and organizations pulled from stored metadata.

No LLM here: Gmail `from` headers and Calendar attendee lists are parsed directly, and each
address is classified as a person or an automated/organizational sender by simple rules.
"""

import re
from dataclasses import dataclass
from email.utils import parseaddr

# Local parts that mean "a system sent this", not a human.
_AUTOMATED_LOCAL = re.compile(
    r"(^|[._+-])(no-?reply|do-?not-?reply|noresponse|notifications?|notify|alerts?|mailer|"
    r"newsletters?|news|updates?|digest|marketing|promo(tions)?|team|support|hello|info|help|"
    r"billing|accounts?|welcome|events?|mail|feedback|community|service|care|contact|admin|"
    r"bounces?|receipts?|orders?|security|verify|invites?|calendar)($|[._+-])",
    re.IGNORECASE,
)
# Subdomains bulk senders use (mail.adobe.com, creator.patreon.com, ...).
_AUTOMATED_SUBDOMAINS = {
    "mail", "email", "e", "em", "news", "newsletter", "marketing", "notifications", "notify",
    "creator", "bounce", "mailer", "info", "updates", "mg", "send", "blog",
}  # fmt: skip
# Calendar resources/group calendars show up as attendees but aren't people.
_NON_PERSON_DOMAINS = ("resource.calendar.google.com", "group.calendar.google.com")
_SECOND_LEVEL = {"co", "com", "ac", "gov", "org", "net", "edu"}


@dataclass(frozen=True)
class Entity:
    key: str  # stable node id: "person:<email>" or "org:<domain>"
    kind: str  # "person" | "organization"
    name: str
    address: str  # email for people, registrable domain for organizations


def registrable_domain(domain: str) -> str:
    """mail.blog.sandcastles.ai → sandcastles.ai; news.bbc.co.uk → bbc.co.uk."""
    labels = domain.lower().strip(".").split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _pretty_local(local: str) -> str:
    words = [w for w in re.split(r"[._+-]+", local) if w and not w.isdigit()]
    return " ".join(w.capitalize() for w in words) or local


def classify_address(raw: str, self_addresses: set[str]) -> Entity | None:
    """Turns '"Priya Nandan" <priya@acme.com>' into an Entity; None for the user themself,
    calendar resources, or anything unparseable."""
    name, address = parseaddr(raw or "")
    address = address.strip().lower()
    if "@" not in address or address in self_addresses:
        return None
    local, _, domain = address.partition("@")
    if domain.endswith(_NON_PERSON_DOMAINS):
        return None
    name = name.strip().strip('"') or ""

    base = registrable_domain(domain)
    subdomains = domain[: -len(base)].rstrip(".").split(".") if domain != base else []
    automated = bool(_AUTOMATED_LOCAL.search(local)) or any(
        label in _AUTOMATED_SUBDOMAINS for label in subdomains
    )
    if automated:
        org_name = name or base.split(".")[0].capitalize()
        return Entity(key=f"org:{base}", kind="organization", name=org_name, address=base)
    return Entity(
        key=f"person:{address}", kind="person", name=name or _pretty_local(local), address=address
    )
