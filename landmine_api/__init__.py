"""FastAPI wrapper around the deterministic landmine screen.

The HTTP layer is a thin shell: it resolves tickers to CIKs, builds the same
providers the CLI uses, calls :func:`landmine.scoring.score_company`, and returns
the same scorecard payload the CLI writes to ``scorecard.json``. No rule logic
lives here, so the API and the CLI always agree.
"""

from .app import app

__all__ = ["app"]
