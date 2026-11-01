"""Load DOL OFLC LCA disclosure files into `lca_filings`.

The Department of Labor publishes every Labor Condition Application quarterly at
https://www.dol.gov/agencies/eta/foreign-labor/performance as .xlsx. Download the
H-1B files you want (one per quarter, several hundred thousand rows each) and point
this at them.

    python -m sponsorship.ingest --db postgresql://... data/LCA_Disclosure_*.xlsx

Column names drift between years, so every field is resolved through an alias list
rather than a fixed name. A file whose columns cannot be resolved fails loudly
instead of silently loading nulls.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from sponsorship.models import Base, LcaFiling
from sponsorship.normalize import normalize_employer

logger = logging.getLogger("sponsorship.ingest")

# Canonical field -> column names seen across DOL releases, newest spelling first.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "case_number": ("CASE_NUMBER", "CASE_NO"),
    "case_status": ("CASE_STATUS", "STATUS"),
    "visa_class": ("VISA_CLASS", "PROGRAM"),
    "employer_name": ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME"),
    "job_title": ("JOB_TITLE", "LCA_CASE_JOB_TITLE", "SOC_TITLE"),
    "soc_code": ("SOC_CODE", "LCA_CASE_SOC_CODE"),
    "worksite_city": ("WORKSITE_CITY", "WORKSITE_CITY_1", "LCA_CASE_WORKLOC1_CITY"),
    "worksite_state": ("WORKSITE_STATE", "WORKSITE_STATE_1", "LCA_CASE_WORKLOC1_STATE"),
    "wage_from": ("WAGE_RATE_OF_PAY_FROM", "WAGE_RATE_OF_PAY_FROM_1", "LCA_CASE_WAGE_RATE_FROM"),
    "wage_unit": ("WAGE_UNIT_OF_PAY", "WAGE_UNIT_OF_PAY_1", "LCA_CASE_WAGE_RATE_UNIT"),
    "decision_date": ("DECISION_DATE", "LCA_CASE_SUBMIT"),
}

REQUIRED = ("case_number", "employer_name")

# Multipliers to put every wage on an annual basis.
_WAGE_UNITS = {
    "year": 1.0, "yr": 1.0, "y": 1.0, "annual": 1.0,
    "month": 12.0, "mth": 12.0, "m": 12.0,
    "bi-weekly": 26.0, "biweekly": 26.0, "bi": 26.0,
    "week": 52.0, "wk": 52.0, "w": 52.0,
    "hour": 2080.0, "hr": 2080.0, "h": 2080.0,
}


def resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map canonical field names onto this file's actual columns."""
    upper = {c.upper().strip(): c for c in df.columns}
    resolved: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in upper:
                resolved[field_name] = upper[alias]
                break
    missing = [f for f in REQUIRED if f not in resolved]
    if missing:
        raise ValueError(
            f"Could not resolve required column(s) {missing}. "
            f"Columns present: {sorted(upper)[:25]}"
        )
    return resolved


def _annualise(wage: object, unit: object) -> float | None:
    try:
        value = float(wage)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    key = str(unit or "year").strip().lower().split()[0] if unit else "year"
    return round(value * _WAGE_UNITS.get(key, 1.0), 2)


def _as_date(value: object) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    return None if pd.isna(ts) else ts.date()


def rows_from_file(path: Path, chunk: int = 50_000) -> Iterator[list[dict]]:
    """Yield batches of LcaFiling kwargs from one disclosure file.

    Only certified H-1B applications are kept. Denied and withdrawn filings say
    nothing useful about whether a company sponsors, and including them inflates
    every count.
    """
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        frames = [pd.read_excel(path, dtype=object)]
    elif suffix in {".csv", ".txt"}:
        frames = pd.read_csv(path, dtype=object, chunksize=chunk, low_memory=False)
    else:
        raise ValueError(f"Unsupported file type: {path.name}")

    cols: dict[str, str] | None = None
    for frame in frames:
        if cols is None:
            cols = resolve_columns(frame)
            logger.info("%s: resolved %d columns", path.name, len(cols))

        batch: list[dict] = []
        for _, row in frame.iterrows():
            status = str(row.get(cols.get("case_status", ""), "") or "").upper()
            if status and not status.startswith("CERTIF"):
                continue
            visa = str(row.get(cols.get("visa_class", ""), "") or "").upper()
            if visa and "H-1B" not in visa and "H1B" not in visa:
                continue

            name = row.get(cols["employer_name"])
            if not name or pd.isna(name):
                continue

            decided = _as_date(row.get(cols.get("decision_date", "")))
            wage = _annualise(
                row.get(cols.get("wage_from", "")), row.get(cols.get("wage_unit", ""))
            )
            state = row.get(cols.get("worksite_state", ""))

            batch.append(
                {
                    "case_number": str(row[cols["case_number"]]).strip()[:32],
                    "case_status": status[:32] or None,
                    "visa_class": visa[:16] or None,
                    "employer_name": str(name).strip()[:300],
                    "employer_key": normalize_employer(str(name))[:300],
                    "job_title": _clip(row.get(cols.get("job_title", "")), 300),
                    "soc_code": _clip(row.get(cols.get("soc_code", "")), 16),
                    "worksite_city": _clip(row.get(cols.get("worksite_city", "")), 120),
                    "worksite_state": _clip(state, 4).upper() if _clip(state, 4) else None,
                    "wage_from": _number(row.get(cols.get("wage_from", ""))),
                    "wage_unit": _clip(row.get(cols.get("wage_unit", "")), 16),
                    "annual_wage": wage,
                    "decision_date": decided,
                    "decision_year": decided.year if decided else None,
                }
            )
            if len(batch) >= chunk:
                yield batch
                batch = []
        if batch:
            yield batch


def _clip(value: object, length: int) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s[:length] if s else None


def _number(value: object) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, 2)


def ingest(db_url: str, paths: list[Path], create: bool = True) -> int:
    """Load files into the database. Returns rows written.

    Idempotent on `case_number`: re-running the same quarter replaces nothing and
    adds nothing, so a partial run can simply be repeated.
    """
    engine = create_engine(db_url, future=True)
    if create:
        Base.metadata.create_all(engine)

    is_pg = engine.dialect.name == "postgresql"
    written = 0

    with Session(engine) as session:
        for path in paths:
            logger.info("reading %s", path)
            for batch in rows_from_file(path):
                if is_pg:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    stmt = pg_insert(LcaFiling).values(batch)
                    stmt = stmt.on_conflict_do_nothing(index_elements=["case_number"])
                else:
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    stmt = sqlite_insert(LcaFiling).values(batch)
                    stmt = stmt.on_conflict_do_nothing(index_elements=["case_number"])
                session.execute(stmt)
                session.commit()
                written += len(batch)
                logger.info("  %s rows", f"{written:,}")

    if is_pg:
        with engine.connect() as conn:
            conn.execute(text("ANALYZE lca_filings"))
            conn.commit()

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load DOL LCA disclosure files.")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--db", required=True, help="SQLAlchemy URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    missing = [p for p in args.files if not p.exists()]
    if missing:
        parser.error(f"file(s) not found: {missing}")

    total = ingest(args.db, args.files)
    logger.info("done, %s rows", f"{total:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
