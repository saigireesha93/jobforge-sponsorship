# Sponsorship engine for JobForge

Answers one question before you spend an hour on a posting: **will this employer
sponsor, and can they put me on payroll in my state?**

Three independent signals, combined into one verdict:

1. **What the posting says.** Regex over the description for refusal and offer
   language. An explicit refusal outranks everything else, because the posting is
   about this role and filing history is about other roles.
2. **What the employer has actually filed.** Every H-1B Labor Condition Application
   the Department of Labor has published, loaded locally. Count, recency, titles,
   states, median wage.
3. **Whether you are eligible where you are.** Detects a state eligibility list in
   the posting and checks whether your state is on it.

No scraping, no API keys, no rate limits. The DOL data is public bulk download and
every lookup is a local index hit.

## Quick start

```bash
pip install sqlalchemy pandas openpyxl pytest
python -m pytest tests/ -q

# load three years of DOL data
python -m sponsorship.ingest --db "sqlite:///lca.db" data/LCA_Disclosure_*.xlsx
```

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sponsorship.service import assess

with Session(create_engine("sqlite:///lca.db")) as s:
    v = assess(s, "Barracuda Networks, Inc.", description=jd_text, home_state="NV")
    print(v.verdict, v.confidence)
    print(v.reason)
```

```
refuses 95
The posting states it will not sponsor.
```

## Why the name normalisation matters

The same company is spelled three ways across DOL data, job boards and offer
letters:

```
BARRACUDA NETWORKS INC
Barracuda Networks, Inc.
Barracuda Networks Inc.
```

All three collapse to `barracuda networks`. Without that, the check returns "no
filings" for a company with sixty-four of them, and the failure is silent. That is
why normalisation is its own module with its own tests.

## Layout

```
sponsorship/
  normalize.py   employer name -> stable lookup key, plus fallback variants
  patterns.py    JD language detection: sponsorship and location eligibility
  models.py      lca_filings table
  ingest.py      DOL disclosure file loader, column-alias tolerant
  service.py     assess() -> Verdict
tests/           34 tests, fixtures taken from real postings
INTEGRATION.md   exactly where this plugs into JobForge
```

## Verdicts

| Verdict | Meaning |
|---|---|
| `refuses` | The posting says it will not sponsor. |
| `unlikely` | No filings, none in three years, or the state list excludes you. |
| `possible` | Filings exist, fewer than five in three years. |
| `likely` | Five or more filings in three years. |

Nothing is hidden automatically. The verdict sorts and filters, and the reason
string is written to be read so you can disagree with it.
