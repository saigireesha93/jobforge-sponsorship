"""Job-description language detection for sponsorship and location eligibility.

Two independent signals come out of the posting text itself, before any database
lookup:

  REFUSES   the posting says, in one of many phrasings, that it will not sponsor
  OFFERS    the posting says it will sponsor, or is E-Verify enrolled
  SILENT    neither, which is the common case

A refusal in the text outranks any filing history. A company with 300 H-1B filings
that writes "this role is not eligible for visa sponsorship" is not sponsoring this
role, and the whole point of the filter is to catch that before an hour is spent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

REFUSES = "refuses"
OFFERS = "offers"
SILENT = "silent"


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Phrasings that mean "we will not sponsor". Deliberately broad: a false positive
# costs one skipped posting, a false negative costs an hour and an application.
_REFUSAL = _compile([
    r"\b(?:not|unable to|cannot|can\s*not|won'?t|will\s+not|do(?:es)?\s+not)\b[^.]{0,60}\bsponsor",
    r"\bno\b[^.]{0,30}\b(?:visa\s+)?sponsorship\b",
    r"\bsponsorship\b[^.]{0,40}\b(?:is\s+)?not\s+(?:available|offered|provided|possible)\b",
    r"\bwithout\s+(?:the\s+)?need\s+for\s+(?:current\s+or\s+future\s+)?sponsorship\b",
    r"\bwithout\s+(?:requiring|the\s+need\s+for)\s+(?:visa\s+)?sponsorship\b",
    r"\bmust\s+be\s+(?:legally\s+)?(?:authori[sz]ed|eligible)\s+to\s+work\b[^.]{0,80}"
    r"\bwithout\b[^.]{0,40}\bsponsorship\b",
    r"\bnot\s+(?:able\s+to\s+)?(?:offer|provide)\b[^.]{0,40}\bsponsorship\b",
    r"\bdoes\s+not\s+provide\b[^.]{0,60}\b(?:visa|work\s+authori[sz]ation)\b",
    r"\bno\s+(?:C2C|corp\s*to\s*corp|third[\s-]party)\b",
])

# Phrasings that mean "we may sponsor". Weaker signal than a refusal, and E-Verify
# enrolment is listed here because it is a prerequisite for STEM OPT employers.
_OFFER = _compile([
    r"\b(?:will|can|do|does|happy\s+to|able\s+to|open\s+to)\b[^.]{0,30}\bsponsor",
    r"\bsponsorship\s+(?:is\s+)?(?:available|offered|provided|considered)\b",
    r"\bvisa\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided)\b",
    r"\bH-?1B\s+(?:sponsorship|transfer|cap[\s-]exempt)\b",
    r"\bwe\s+sponsor\b",
    r"\bE-?Verify\s+(?:employer|participant|enrolled)\b",
    r"\bis\s+an\s+E-?Verify\s+employer\b",
])

# A refusal pattern can fire inside a sentence that is actually an EEO boilerplate
# disclaimer or a reassurance. These cancel a hit on the same sentence.
_REFUSAL_EXCLUSIONS = _compile([
    r"\bequal\s+opportunity\b",
    r"\bregardless\s+of\b[^.]{0,60}\bcitizenship\b",
])

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?\n]?")


@dataclass
class TextSignal:
    """What the posting text alone says about sponsorship."""

    signal: str = SILENT
    evidence: list[str] = field(default_factory=list)

    @property
    def refuses(self) -> bool:
        return self.signal == REFUSES


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def scan_text(description: str | None, max_evidence: int = 3) -> TextSignal:
    """Classify a job description's sponsorship language.

    Matching runs per sentence so the 60-character windows in the patterns cannot
    span unrelated clauses, and so the evidence returned is a quotable fragment the
    user can read rather than an offset.
    """
    if not description or not description.strip():
        return TextSignal()

    refusals: list[str] = []
    offers: list[str] = []

    for sentence in _sentences(description):
        if any(x.search(sentence) for x in _REFUSAL_EXCLUSIONS):
            continue
        if any(p.search(sentence) for p in _REFUSAL):
            refusals.append(_trim(sentence))
        elif any(p.search(sentence) for p in _OFFER):
            offers.append(_trim(sentence))

    if refusals:
        return TextSignal(REFUSES, refusals[:max_evidence])
    if offers:
        return TextSignal(OFFERS, offers[:max_evidence])
    return TextSignal()


def _trim(sentence: str, limit: int = 220) -> str:
    sentence = re.sub(r"\s+", " ", sentence).strip()
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


# --------------------------------------------------------------------- location

_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}

_ONSITE = _compile([
    r"\b(\d+)\s*(?:\+)?\s*days?\s+(?:per|a)\s+week\s+(?:in[\s-]?office|on[\s-]?site)\b",
    r"\bonsite\s+(\w+)\s+through\s+(\w+)\b",
    r"\bhybrid\b[^.]{0,40}\b(\d+)\s*days?\b",
    r"\bmust\s+(?:live|reside|be\s+located)\s+within\s+(\d+)\s*miles\b",
    r"\bthis\s+is\s+(?:an?\s+)?(?:fully\s+)?on[\s-]?site\s+(?:role|position)\b",
])

_REMOTE = _compile([
    r"\b(?:fully\s+|100%\s+)?remote\b",
    r"\bwork\s+from\s+(?:home|anywhere)\b",
])


@dataclass
class LocationSignal:
    """Eligibility read off the posting's own location language."""

    remote: bool = False
    onsite_required: bool = False
    eligible_states: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def excludes(self, state: str) -> bool:
        """True when the posting names an eligibility list that omits `state`."""
        return bool(self.eligible_states) and state.upper() not in self.eligible_states


def scan_location(description: str | None, min_list_size: int = 3) -> LocationSignal:
    """Detect remote/onsite language and any state eligibility list.

    A posting naming three or more US states in one region of the text is treated as
    an eligibility list. That is the Google pattern: nine cities, no Nevada, which is
    a hard stop that is easy to miss by reading.
    """
    out = LocationSignal()
    if not description:
        return out

    for sentence in _sentences(description):
        for p in _ONSITE:
            if p.search(sentence):
                out.onsite_required = True
                out.evidence.append(_trim(sentence))
                break
        if not out.remote:
            for p in _REMOTE:
                if p.search(sentence):
                    out.remote = True
                    break

    found: list[str] = []
    lowered = description.lower()
    for full, abbr in _STATE_ABBR.items():
        if re.search(rf"\b{re.escape(full)}\b", lowered):
            found.append(abbr)
    # "New York, NY, USA" / "Boulder, CO;" — the comma anchors it, so no lookbehind.
    # A bare "CA" in prose is ignored because it needs the preceding comma.
    for abbr in set(_STATE_ABBR.values()):
        if re.search(rf",\s*{abbr}\b", description):
            found.append(abbr)

    unique = sorted(set(found))
    if len(unique) >= min_list_size:
        out.eligible_states = unique
    return out
