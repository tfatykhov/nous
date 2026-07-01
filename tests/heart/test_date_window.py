import datetime
from nous.heart.date_window import DateWindow, has_temporal_signal

def test_temporal_signal_detects_month_year():
    assert has_temporal_signal("What happened in late April 2026?") is True
    assert has_temporal_signal("changes around mid-May") is True
    assert has_temporal_signal("events on 2026-06-24") is True

def test_temporal_signal_rejects_non_temporal():
    assert has_temporal_signal("How does the calibration gate work?") is False
    assert has_temporal_signal("summarize the trading bot design") is False

def test_datewindow_is_frozen():
    w = DateWindow(start=datetime.date(2026, 4, 20), end=datetime.date(2026, 4, 30))
    assert w.start < w.end
