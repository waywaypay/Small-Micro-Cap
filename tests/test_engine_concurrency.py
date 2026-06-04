"""Fanning the screen across worker threads must stay byte-identical to serial.

The parallel path exists only to go faster; it must not change *what* the screen
produces. ``scorecards_to_payload`` re-sorts by ticker, so completion order can
never leak into the output — this pins that guarantee.
"""
import datetime as dt

from landmine_api.engine import Settings, screen_tickers


def _settings(workers: int) -> Settings:
    # fixture source -> fully offline and deterministic (no SEC egress)
    return Settings(source="fixture", screen_workers=workers)


def test_parallel_screen_matches_serial_and_is_ticker_sorted():
    as_of = dt.date(2026, 6, 2)
    tickers = ["WKHS", "AAPL", "BYND", "AMC"]

    serial = screen_tickers(tickers, as_of, _settings(1))
    parallel = screen_tickers(tickers, as_of, _settings(8))

    assert parallel == serial
    assert [c["ticker"] for c in parallel] == ["AAPL", "AMC", "BYND", "WKHS"]
