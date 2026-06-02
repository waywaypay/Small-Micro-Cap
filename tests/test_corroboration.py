"""Cross-tier corroboration of the cash-runway flag with Tier-2 events."""
import datetime as dt

from landmine.corroboration import corroborate
from landmine.events import Event, EventSet, EventType
from landmine.models import Citation, RuleResult, Severity, Status

CFG = {"enabled": True, "R2_CASH_RUNWAY": {
    "corroborating_events": ["GOING_CONCERN", "OFFERING", "LATE_FILING"],
    "within_days": 540, "downgrade_uncorroborated": False}}


def _r2_flag(sev=Severity.CRITICAL, score=0.9):
    cite = Citation("Cash", "2026-03-31", "2026-05-14", "10-Q", "x")
    return RuleResult("R2_CASH_RUNWAY", "R2_CASH_RUNWAY_SHORT", Status.FLAG, sev,
                      score, {"runway_quarters": 0.1}, {}, [cite], 0.1)


def _view(as_of, events):
    return EventSet("X", "1", events).as_of(as_of)


def test_corroborated_when_event_present():
    ev = [Event(EventType.GOING_CONCERN, dt.date(2026, 3, 31), "10-K")]
    out = corroborate([_r2_flag()], _view(dt.date(2026, 6, 2), ev), CFG)
    corr = out[0].raw_values["corroboration"]
    assert corr["corroborated"] is True and "GOING_CONCERN" in corr["by"]
    assert out[0].status is Status.FLAG and out[0].severity is Severity.CRITICAL


def test_uncorroborated_annotates_only_by_default():
    out = corroborate([_r2_flag()], _view(dt.date(2026, 6, 2), []), CFG)
    assert out[0].raw_values["corroboration"]["corroborated"] is False
    assert out[0].severity is Severity.CRITICAL          # score untouched by default


def test_downgrade_uncorroborated_caps_severity():
    cfg = {"enabled": True, "R2_CASH_RUNWAY": dict(
        CFG["R2_CASH_RUNWAY"], downgrade_uncorroborated=True,
        uncorroborated_severity="MEDIUM", uncorroborated_score_cap=0.5)}
    out = corroborate([_r2_flag()], _view(dt.date(2026, 6, 2), []), cfg)
    assert out[0].severity is Severity.MEDIUM and out[0].severity_score <= 0.5
    assert out[0].reason == "R2_CASH_RUNWAY_UNCORROBORATED"


def test_events_are_point_in_time():
    # a going concern filed AFTER as_of does not corroborate
    ev = [Event(EventType.GOING_CONCERN, dt.date(2026, 8, 1), "10-K")]
    out = corroborate([_r2_flag()], _view(dt.date(2026, 6, 2), ev), CFG)
    assert out[0].raw_values["corroboration"]["corroborated"] is False


def test_only_flagged_r2_is_touched():
    other = RuleResult("R3_NEGATIVE_EQUITY", "x", Status.FLAG, Severity.HIGH, 0.5,
                       {}, {}, [], None)
    out = corroborate([other], _view(dt.date(2026, 6, 2), []), CFG)
    assert "corroboration" not in out[0].raw_values


def test_disabled_is_noop():
    out = corroborate([_r2_flag()], _view(dt.date(2026, 6, 2), []),
                      {"enabled": False})
    assert "corroboration" not in out[0].raw_values
