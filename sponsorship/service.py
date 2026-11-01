"""The sponsorship verdict: does this employer sponsor, and can they hire me here.

Combines three independent signals:

  1. what the posting text says            (patterns.scan_text)
  2. what the employer has actually filed  (LCA history)
  3. whether the posting's location list includes the user's state

Ordering matters. An explicit refusal in the text beats any filing history, because
the posting is about this role and the history is about other roles. Everything else
is evidence weighted by recency and volume.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sponsorship.models import LcaFiling
from sponsorship.normalize import name_variants, normalize_employer
from sponsorship.patterns import OFFERS, REFUSES, scan_location, scan_text

# Verdicts, worst to best.
REFUSES_V = "refuses"
UNLIKELY = "unlikely"
POSSIBLE = "possible"
LIKELY = "likely"
UNKNOWN = "unknown"

# A filing older than this says little about whether they sponsor today.
RECENT_YEARS = 3
# Below this many recent filings, a company is sponsoring occasionally, not routinely.
ROUTINE_THRESHOLD = 5


@dataclass
class EmployerHistory:
    """What the LCA table knows about one employer."""

    matched_key: str | None = None
    total: int = 0
    recent: int = 0
    last_year: int | None = None
    in_state: int = 0
    top_titles: list[tuple[str, int]] = field(default_factory=list)
    states: dict[str, int] = field(default_factory=dict)
    median_wage: float | None = None

    @property
    def found(self) -> bool:
        return self.total > 0


@dataclass
class Verdict:
    """The answer, plus everything needed to explain it on a job card."""

    verdict: str
    confidence: int
    reason: str
    history: EmployerHistory
    text_signal: str
    text_evidence: list[str] = field(default_factory=list)
    location_eligible: bool | None = None
    location_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["history"] = asdict(self.history)
        return d


def lookup_employer(
    session: Session,
    employer_name: str,
    home_state: str | None = None,
    today: date | None = None,
) -> EmployerHistory:
    """Find an employer's filing history, trying progressively shorter name keys.

    Returns the first variant that produces any rows. "barracuda networks inc" fails,
    "barracuda networks" hits, so the caller never has to guess the registered name.
    """
    today = today or date.today()
    cutoff = today.year - RECENT_YEARS

    for key in name_variants(employer_name):
        rows = session.execute(
            select(
                LcaFiling.job_title,
                LcaFiling.worksite_state,
                LcaFiling.decision_year,
                LcaFiling.annual_wage,
            ).where(LcaFiling.employer_key == key)
        ).all()
        if not rows:
            continue

        titles: dict[str, int] = {}
        states: dict[str, int] = {}
        wages: list[float] = []
        recent = 0
        last_year: int | None = None

        for title, state, year, wage in rows:
            if title:
                titles[title] = titles.get(title, 0) + 1
            if state:
                states[state] = states.get(state, 0) + 1
            if year:
                last_year = year if last_year is None else max(last_year, year)
                if year >= cutoff:
                    recent += 1
            if wage:
                wages.append(float(wage))

        wages.sort()
        median = wages[len(wages) // 2] if wages else None

        return EmployerHistory(
            matched_key=key,
            total=len(rows),
            recent=recent,
            last_year=last_year,
            in_state=states.get((home_state or "").upper(), 0),
            top_titles=sorted(titles.items(), key=lambda kv: -kv[1])[:8],
            states=states,
            median_wage=median,
        )

    return EmployerHistory()


def assess(
    session: Session,
    employer_name: str,
    description: str | None = None,
    home_state: str | None = None,
    today: date | None = None,
) -> Verdict:
    """Produce the verdict shown on a job card.

    `home_state` is the two-letter state the user can actually work from. It does not
    gate the verdict, it adds the "have they ever hired here" fact, which is the one
    that caught UnityPoint and Remitly.
    """
    text = scan_text(description)
    loc = scan_location(description)
    history = lookup_employer(session, employer_name, home_state=home_state, today=today)

    location_eligible: bool | None = None
    if home_state and loc.eligible_states:
        location_eligible = not loc.excludes(home_state)

    # 1. An explicit refusal ends it.
    if text.refuses:
        return Verdict(
            verdict=REFUSES_V,
            confidence=95,
            reason="The posting states it will not sponsor.",
            history=history,
            text_signal=text.signal,
            text_evidence=text.evidence,
            location_eligible=location_eligible,
            location_evidence=loc.evidence,
        )

    # 2. No filing history at all.
    if not history.found:
        offered = text.signal == OFFERS
        return Verdict(
            verdict=POSSIBLE if offered else UNLIKELY,
            confidence=45 if offered else 65,
            reason=(
                "The posting mentions sponsorship but this employer has no H-1B filings on record."
                if offered
                else "No H-1B filings on record for this employer."
            ),
            history=history,
            text_signal=text.signal,
            text_evidence=text.evidence,
            location_eligible=location_eligible,
            location_evidence=loc.evidence,
        )

    # 3. Filing history exists. Weigh recency and volume.
    stale = history.recent == 0
    routine = history.recent >= ROUTINE_THRESHOLD

    if stale:
        verdict, confidence = UNLIKELY, 55
        reason = (
            f"{history.total} H-1B filings on record but none since {history.last_year}."
        )
    elif routine:
        verdict, confidence = LIKELY, 85
        reason = (
            f"{history.recent} H-1B filings in the last {RECENT_YEARS} years "
            f"({history.total} total), most recently {history.last_year}."
        )
    else:
        verdict, confidence = POSSIBLE, 65
        reason = (
            f"{history.recent} H-1B filings in the last {RECENT_YEARS} years "
            f"({history.total} total). Sponsors occasionally rather than routinely."
        )

    if text.signal == OFFERS:
        confidence = min(95, confidence + 10)
        reason += " The posting also mentions sponsorship."

    if home_state:
        if history.in_state:
            reason += f" {history.in_state} of those filings are in {home_state.upper()}."
        else:
            reason += f" None are in {home_state.upper()}."
            confidence = max(20, confidence - 10)

    if location_eligible is False:
        verdict = UNLIKELY
        reason += (
            f" The posting names an eligibility list that does not include "
            f"{home_state.upper()}."
        )

    return Verdict(
        verdict=verdict,
        confidence=confidence,
        reason=reason,
        history=history,
        text_signal=text.signal,
        text_evidence=text.evidence,
        location_eligible=location_eligible,
        location_evidence=loc.evidence,
    )


def employer_key(name: str) -> str:
    """Re-exported so the ingest and the migration use one implementation."""
    return normalize_employer(name)
