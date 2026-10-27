"""Build the company → roles catalog fed into classify prompts.

Grows as we process mail. Reusing keys from here is how Stepstone
confirmations attach to Dive instead of inventing company=Stepstone.
"""

from __future__ import annotations

from collections import defaultdict

from jobmail.models import Application, Event
from jobmail.normalize import normalize_company


def collect_catalog_keys(
    applications: list[Application] | None = None,
    events: list[Event] | None = None,
) -> set[str]:
    """Known application_key values the classifier is allowed to reuse as-is."""
    keys: set[str] = set()
    for app in applications or []:
        key = (app.application_key or "").strip().lower()
        if key and not key.startswith("unknown|"):
            keys.add(key)
    for event in events or []:
        if event.event_type == "newsletter_noise":
            continue
        key = (event.application_key or "").strip().lower()
        if key and not key.startswith("unknown|"):
            keys.add(key)
    return keys


def build_catalog_text(
    applications: list[Application] | None = None,
    events: list[Event] | None = None,
) -> str:
    """Render known apps as nested company/roles for the LLM."""
    by_company: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen_keys: set[str] = set()

    for app in applications or []:
        key = (app.application_key or "").strip()
        if not key or key in seen_keys:
            continue
        if key.startswith("unknown|"):
            continue
        seen_keys.add(key)
        company = app.company or key.split("|", 1)[0]
        role = app.role_title or (key.split("|", 1)[1] if "|" in key else "")
        by_company[_company_label(company)].append((role, key))

    # events fill gaps before we've clustered this run
    for event in events or []:
        key = (event.application_key or "").strip()
        if not key or key in seen_keys:
            continue
        if key.startswith("unknown|"):
            continue
        if event.event_type == "newsletter_noise":
            continue
        seen_keys.add(key)
        company = event.company or key.split("|", 1)[0]
        role = event.role_title or (key.split("|", 1)[1] if "|" in key else "")
        by_company[_company_label(company)].append((role, key))

    if not by_company:
        return (
            "(none yet — leave application_key empty; we derive it from company + role)"
        )

    lines = ["Known applications (reuse key=… exactly when this mail belongs to one):"]
    for company in sorted(by_company, key=lambda c: normalize_company(c)):
        lines.append(f"- company: {company}")
        lines.append("  roles:")
        # de-dupe roles by key
        roles = {}
        for role, key in by_company[company]:
            roles[key] = role
        for key, role in sorted(roles.items(), key=lambda item: item[1].lower()):
            lines.append(f"    - {role}  key={key}")
    return "\n".join(lines)


def _company_label(company: str) -> str:
    return company.strip() or "Unknown"
