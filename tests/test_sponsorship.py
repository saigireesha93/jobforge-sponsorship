"""Tests for the sponsorship engine.

The fixtures use real strings from job descriptions in this project's history,
because the failure modes that matter are the ones that already happened.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sponsorship.models import Base, LcaFiling
from sponsorship.normalize import name_variants, normalize_employer
from sponsorship.patterns import OFFERS, REFUSES, SILENT, scan_location, scan_text
from sponsorship.service import (
    LIKELY, POSSIBLE, REFUSES_V, UNLIKELY, assess, lookup_employer,
)

TODAY = date(2026, 11, 1)


# ------------------------------------------------------------------ normalize

@pytest.mark.parametrize("raw,expected", [
    ("Barracuda Networks, Inc.", "barracuda networks"),
    ("BARRACUDA NETWORKS INC", "barracuda networks"),
    ("Barracuda Networks Inc.", "barracuda networks"),
    ("Tesla, Inc.", "tesla"),
    ("NOVANT HEALTH INC", "novant health"),
    ("Genex Services, LLC", "genex"),
    ("Pharming Healthcare Inc", "pharming healthcare"),
    ("", ""),
    (None, ""),
])
def test_normalize_collapses_spellings(raw, expected):
    assert normalize_employer(raw) == expected


def test_name_made_only_of_suffixes_does_not_empty():
    # Must not return "" and therefore match every row in the table.
    assert normalize_employer("The Company LLC") != ""


def test_variants_narrow_from_the_right():
    # "group" is a corporate suffix and is stripped before variants are built, so the
    # DOL spelling "NOVANT HEALTH MEDICAL GROUP" and a board's "Novant Health" meet.
    v = name_variants("Novant Health Medical Group")
    assert v[0] == "novant health medical"
    assert "novant health" in v
    assert v[-1] == "novant"


# ------------------------------------------------------------------- patterns

@pytest.mark.parametrize("text", [
    "Barracuda does not provide visa or work authorization sponsorship for this role, now or in the future.",
    "We are unable to sponsor or take over sponsorship of an employment visa at this time.",
    "Applicants must be authorized to work in the US without sponsorship.",
    "No visa sponsorship is available for this position.",
    "Candidates must be able to work without the need for current or future sponsorship.",
    "Sponsorship is not offered for this opening.",
])
def test_refusal_phrasings_detected(text):
    assert scan_text(text).signal == REFUSES


@pytest.mark.parametrize("text", [
    "Remitly is an E-Verify Employer",
    "We will sponsor H-1B transfers for exceptional candidates.",
    "Visa sponsorship is available for this role.",
])
def test_offer_phrasings_detected(text):
    assert scan_text(text).signal == OFFERS


def test_eeo_boilerplate_is_not_a_refusal():
    text = (
        "We are an equal opportunity employer and do not discriminate regardless of "
        "citizenship or national origin."
    )
    assert scan_text(text).signal != REFUSES


def test_silent_posting():
    assert scan_text("Senior Data Engineer. You will build pipelines.").signal == SILENT


def test_refusal_outranks_offer_in_same_posting():
    text = "We are an E-Verify employer. This role does not offer visa sponsorship."
    assert scan_text(text).signal == REFUSES


def test_refusal_returns_quotable_evidence():
    sig = scan_text("Barracuda does not provide visa sponsorship for this role.")
    assert sig.evidence and "sponsorship" in sig.evidence[0].lower()


# ------------------------------------------------------------------- location

def test_state_eligibility_list_detected():
    # The Google posting: nine cities, no Nevada.
    jd = (
        "preferred working location from the following: New York, NY, USA; Atlanta, GA, "
        "USA; Boulder, CO, USA; Chicago, IL, USA; Mountain View, CA, USA."
    )
    loc = scan_location(jd)
    assert loc.eligible_states
    assert loc.excludes("NV")
    assert not loc.excludes("CA")


def test_onsite_requirement_detected():
    loc = scan_location("employees in this position work onsite Tuesday through Thursday each week.")
    assert loc.onsite_required


def test_remote_detected():
    assert scan_location("Location: Remote US").remote


# -------------------------------------------------------------------- service

@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add(session, name, n, year, state="CA", title="DATA ENGINEER"):
    for i in range(n):
        session.add(LcaFiling(
            case_number=f"{name[:6]}-{year}-{state}-{i}",
            case_status="CERTIFIED", visa_class="H-1B",
            employer_name=name, employer_key=normalize_employer(name),
            job_title=title, worksite_state=state,
            decision_date=date(year, 6, 1), decision_year=year,
            annual_wage=120000,
        ))
    session.commit()


def test_lookup_matches_across_spellings(session):
    _add(session, "BARRACUDA NETWORKS INC", 12, 2026)
    h = lookup_employer(session, "Barracuda Networks, Inc.", today=TODAY)
    assert h.found and h.total == 12


def test_routine_sponsor_is_likely(session):
    _add(session, "TESLA INC", 40, 2026, state="NV")
    v = assess(session, "Tesla, Inc.", "Build data pipelines.", home_state="NV", today=TODAY)
    assert v.verdict == LIKELY
    assert v.history.in_state == 40
    assert "NV" in v.reason


def test_occasional_sponsor_is_possible(session):
    _add(session, "PHARMING HEALTHCARE INC", 2, 2026, state="NJ")
    v = assess(session, "Pharming Healthcare Inc", "", home_state="NV", today=TODAY)
    assert v.verdict == POSSIBLE
    assert "None are in NV" in v.reason


def test_stale_history_is_unlikely(session):
    _add(session, "OLDCO INC", 50, 2018)
    v = assess(session, "OldCo Inc", "", today=TODAY)
    assert v.verdict == UNLIKELY
    assert "2018" in v.reason


def test_no_history_is_unlikely(session):
    v = assess(session, "Nex Team Inc", "A startup building motion games.", today=TODAY)
    assert v.verdict == UNLIKELY
    assert v.history.total == 0


def test_explicit_refusal_beats_heavy_history(session):
    _add(session, "BARRACUDA NETWORKS INC", 64, 2026)
    v = assess(
        session, "Barracuda Networks, Inc.",
        "Barracuda does not provide visa or work authorization sponsorship for this role.",
        home_state="NV", today=TODAY,
    )
    assert v.verdict == REFUSES_V
    assert v.confidence >= 90
    # History is still reported, so the user sees the contradiction.
    assert v.history.total == 64


def test_eligibility_list_without_home_state_downgrades(session):
    _add(session, "GOOGLE LLC", 98, 2026, state="CA")
    jd = "locations: New York, NY; Atlanta, GA; Boulder, CO; Chicago, IL; Mountain View, CA."
    v = assess(session, "Google LLC", jd, home_state="NV", today=TODAY)
    assert v.location_eligible is False
    assert v.verdict == UNLIKELY
