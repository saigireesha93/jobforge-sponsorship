"""Storage for DOL LCA disclosure data and the per-posting verdict.

One table holds every H-1B Labor Condition Application the Department of Labor has
published. It is append-only, rebuilt per quarter, and indexed on the normalised
employer key because that is the only column anything looks up by.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LcaFiling(Base):
    """One Labor Condition Application as published by DOL OFLC.

    Only the columns the sponsorship check actually reads are kept. The disclosure
    files carry roughly ninety columns; storing them all would quadruple the table
    for data nothing queries.
    """

    __tablename__ = "lca_filings"
    __table_args__ = (
        # The hot path: every lookup is employer_key, optionally narrowed by state.
        Index("idx_lca_employer_key", "employer_key"),
        Index("idx_lca_employer_state", "employer_key", "worksite_state"),
        Index("idx_lca_employer_year", "employer_key", "decision_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    case_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    case_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visa_class: Mapped[str | None] = mapped_column(String(16), nullable=True)

    employer_name: Mapped[str] = mapped_column(String(300), nullable=False)
    # normalize_employer(employer_name). Never query on employer_name directly.
    employer_key: Mapped[str] = mapped_column(String(300), nullable=False)

    job_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    soc_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    worksite_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    worksite_state: Mapped[str | None] = mapped_column(String(4), nullable=True)

    wage_from: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    wage_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # wage_from converted to an annual figure, so comparisons do not have to
    # re-derive it from hourly/weekly/monthly rows every query.
    annual_wage: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    decision_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --------------------------------------------------------------------------- verdict
# Columns to add to JobForge's existing `discovered_jobs` and `jobs` tables. Kept here
# as a reference block so the Alembic revision and the dataclass stay in step; see
# INTEGRATION.md for the migration.
SPONSORSHIP_COLUMNS = """
    sponsorship_verdict     VARCHAR(16)   -- refuses | unlikely | possible | likely | unknown
    sponsorship_confidence  SMALLINT      -- 0-100
    sponsorship_reason      TEXT          -- human-readable, shown on the card
    h1b_filings_total       INTEGER
    h1b_filings_recent      INTEGER       -- within RECENT_YEARS
    h1b_last_year           SMALLINT
    h1b_filings_in_state    INTEGER       -- user's own state
    location_eligible       BOOLEAN       -- NULL when the posting says nothing
"""
