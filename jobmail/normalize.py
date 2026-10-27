"""Normalization helpers for wrangling + application_key.

Display fields stay as-written. Norms are lowercase, fluff-stripped,
stable enough to join on in a spreadsheet later.
"""

from __future__ import annotations

import re
import unicodedata

_LEGAL_FLUFF = re.compile(
    r"\b(gmbh|ag|ltd|llc|inc|se|kg|mbh|co|corp|corporation|ug)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")
# unwrap (…) but keep the inner words for the key
_PARENS = re.compile(r"\(([^)]*)\)")
# team/org tails: "Title – Team" / "Title - Org" / em-dash
_TAIL_SPLIT = re.compile(r"\s+[–—-]\s+")
_GENDER = re.compile(
    r"\b(m/w/d|m/f/d|w/m/d|f/m/d|m/w/x|m/f/x|f/m/x|mwd|mfd)\b",
    re.IGNORECASE,
)


def normalize_company(value: str) -> str:
    text = _fold(value)
    text = _LEGAL_FLUFF.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_role(value: str) -> str:
    """Tighten role text for keys / role_title_norm.

    Keeps display role_title elsewhere. Here we:
    - drop gender tokens
    - unwrap (parentheses) so (Fullstack) → fullstack
      (commas inside parens become spaces so they aren't title tails)
    - cut team/org tails after spaced dash
    - take the head before a comma (drops ', Dmarts …')
    """
    text = _fold(value)
    text = _GENDER.sub(" ", text)

    def _unwrap(match: re.Match[str]) -> str:
        inner = match.group(1).replace(",", " ")
        return f" {inner} "

    text = _PARENS.sub(_unwrap, text)
    # first clause only — team/location suffixes after comma are noisy for keys
    text = text.split(",", 1)[0]
    text = _TAIL_SPLIT.split(text, maxsplit=1)[0]
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_skill(value: str) -> str:
    return _fold(value).strip()


def normalize_skills(skills: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        norm = normalize_skill(str(skill))
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def application_key(company: str, role_title: str) -> str:
    """Build key from display strings via norms."""
    c = normalize_company(company)
    r = normalize_role(role_title)
    if not c and not r:
        return ""
    return f"{c}|{r}"


def resolve_application_key(
    company: str,
    role_title: str,
    llm_key: str | None,
    catalog_keys: set[str] | None = None,
) -> str:
    """Reuse an exact catalog key from the LLM; otherwise derive from norms."""
    candidate = (llm_key or "").strip().lower()
    known = catalog_keys or set()
    if candidate and candidate in known:
        return candidate
    return application_key(company, role_title)


def _fold(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    return text.lower().strip()
