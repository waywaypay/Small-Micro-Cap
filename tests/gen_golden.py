"""Regenerate the golden scorecard snapshot. Run deliberately after an
intended change in rule output:  python -m tests.gen_golden
"""
from .test_golden import GOLDEN, render

if __name__ == "__main__":
    with open(GOLDEN, "w", encoding="utf-8") as fh:
        fh.write(render())
    print(f"Wrote {GOLDEN}")
