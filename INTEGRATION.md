# Wiring this into JobForge

Five changes. Every one follows a pattern already in the codebase, so nothing here
fights the existing design.

## 1. Drop the package in

```
backend/app/services/sponsorship/   <- this package
backend/tests/test_sponsorship.py   <- the tests
```

Change the imports from `sponsorship.` to `app.services.sponsorship.` and point
`models.Base` at JobForge's `app.database.Base` instead of its own.

Dependencies: `pandas` and `openpyxl`, ingest-only. Add with `uv add`, never pip,
per the project's CLAUDE.md.

## 2. Alembic revision

Expand-only, idempotent, no drops or type narrowing, matching the project's rule.

```python
"""sponsorship signals

Revision ID: 0006_sponsorship
Revises: 0005_discovery
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_sponsorship"
down_revision = "0005_discovery"


def upgrade() -> None:
    op.create_table(
        "lca_filings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("case_number", sa.String(32), nullable=False, unique=True),
        sa.Column("case_status", sa.String(32)),
        sa.Column("visa_class", sa.String(16)),
        sa.Column("employer_name", sa.String(300), nullable=False),
        sa.Column("employer_key", sa.String(300), nullable=False),
        sa.Column("job_title", sa.String(300)),
        sa.Column("soc_code", sa.String(16)),
        sa.Column("worksite_city", sa.String(120)),
        sa.Column("worksite_state", sa.String(4)),
        sa.Column("wage_from", sa.Numeric(12, 2)),
        sa.Column("wage_unit", sa.String(16)),
        sa.Column("annual_wage", sa.Numeric(12, 2)),
        sa.Column("decision_date", sa.Date),
        sa.Column("decision_year", sa.Integer),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_lca_employer_key", "lca_filings", ["employer_key"])
    op.create_index("idx_lca_employer_state", "lca_filings", ["employer_key", "worksite_state"])
    op.create_index("idx_lca_employer_year", "lca_filings", ["employer_key", "decision_year"])

    for table in ("discovered_jobs", "jobs"):
        op.add_column(table, sa.Column("sponsorship_verdict", sa.String(16)))
        op.add_column(table, sa.Column("sponsorship_confidence", sa.SmallInteger))
        op.add_column(table, sa.Column("sponsorship_reason", sa.Text))
        op.add_column(table, sa.Column("h1b_filings_total", sa.Integer))
        op.add_column(table, sa.Column("h1b_filings_recent", sa.Integer))
        op.add_column(table, sa.Column("h1b_last_year", sa.SmallInteger))
        op.add_column(table, sa.Column("h1b_filings_in_state", sa.Integer))
        op.add_column(table, sa.Column("location_eligible", sa.Boolean))

    op.create_index(
        "idx_discovered_sponsorship",
        "discovered_jobs",
        ["workspace_id", "sponsorship_verdict"],
    )


def downgrade() -> None:
    # Expand-only schema: nothing is dropped.
    pass
```

## 3. Score at discovery time

`services/discovery.py` already builds a dict per posting around line 160, where it
calls `keyword_score()`. Add one call beside it. Both are cheap and deterministic, so
this costs nothing per run.

```python
from app.services.sponsorship.service import assess

# inside the per-posting loop, next to "keyword_score": keyword_score(...)
verdict = assess(
    session,
    employer_name=p.company,
    description=p.description,
    home_state=workspace_settings.home_state,
)

row.update({
    "sponsorship_verdict":    verdict.verdict,
    "sponsorship_confidence": verdict.confidence,
    "sponsorship_reason":     verdict.reason,
    "h1b_filings_total":      verdict.history.total,
    "h1b_filings_recent":     verdict.history.recent,
    "h1b_last_year":          verdict.history.last_year,
    "h1b_filings_in_state":   verdict.history.in_state,
    "location_eligible":      verdict.location_eligible,
})
```

Do the same in `services/ingest.py` so extension-saved and manually pasted jobs get a
verdict too, not only discovered ones.

## 4. Filter in the inbox

`services/discovery_inbox.py` lines 81 to 100 already implement a `min_fit` filter.
This follows the identical shape:

```python
# alongside the existing min_fit handling
if exclude_refusals:
    conditions.append(
        DiscoveredJob.sponsorship_verdict.notin_(("refuses", "unlikely"))
    )
if require_state_eligible:
    conditions.append(
        or_(
            DiscoveredJob.location_eligible.is_(None),
            DiscoveredJob.location_eligible.is_(True),
        )
    )
```

Expose both as query params on `routers/discovery.py`, and as two switches in the
discovery UI: "Hide employers who will not sponsor" and "Hide jobs I am not eligible
for in my state".

## 5. Settings and env

`workspace_settings` needs one new field, `home_state`, a two-letter code. Without it
the in-state counts are skipped and everything else still works.

Per CLAUDE.md, any new env var needs a `${VAR:-default}` line in the `x-backend-env`
anchor in `docker-compose.yml` **and** a matching line in the `.env` heredoc in
`.github/workflows/ci.yml`. This package needs none, which is deliberate.

---

## Loading the data

The Department of Labor publishes every Labor Condition Application quarterly:

https://www.dol.gov/agencies/eta/foreign-labor/performance

Download the H-1B disclosure files. Each quarter is a few hundred thousand rows.
Three years of history is enough for the `recent` window to be meaningful.

```bash
python -m sponsorship.ingest --db "$DATABASE_URL" data/LCA_Disclosure_*.xlsx
```

Idempotent on `case_number`, so re-running a quarter is safe and a partial run can
simply be repeated. Expect roughly 2 to 4 million rows for three years, which is
about 600MB in Postgres with the indexes.

Schedule a Celery Beat task to pull each new quarter. DOL publishes about six weeks
after quarter end.

## Verdicts

| Verdict | Meaning |
|---|---|
| `refuses` | The posting says in words that it will not sponsor. Outranks everything. |
| `unlikely` | No filings, or none in the last three years, or the state list excludes you. |
| `possible` | Filings exist but fewer than five in three years. Sponsors occasionally. |
| `likely` | Five or more filings in three years. Sponsors routinely. |

The verdict never hides a posting by itself. It sorts and filters, and the reason
string is written to be read on the card so you can disagree with it.
