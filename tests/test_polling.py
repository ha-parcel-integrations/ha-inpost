"""Tests for InPost's unconditional dynamic polling policy."""
from datetime import datetime, timedelta, timezone

from custom_components.inpost.const import (
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.inpost.coordinator import (
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)

UTC = timezone.utc


def test_quiet_window_and_anchors_use_local_clock():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert _next_anchor(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, 6, tzinfo=UTC
    )


def test_stagger_is_stable_and_bounded():
    assert _stagger_minutes("entry-1") == _stagger_minutes("entry-1")
    assert 0 <= _stagger_minutes("entry-1") < STAGGER_MINUTES


def test_account_model_keeps_mid_polling_when_empty():
    assert _hottest_tier_minutes(
        [], datetime(2026, 1, 1, 12, tzinfo=UTC), stop_when_empty=False
    ) == MID_INTERVAL_MINUTES


def test_tracking_model_suspends_when_empty():
    assert (
        _hottest_tier_minutes(
            [], datetime(2026, 1, 1, 12, tzinfo=UTC), stop_when_empty=True
        )
        is None
    )


def test_out_for_delivery_without_eta_is_hot():
    assert _hottest_tier_minutes(
        [{"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": None}],
        datetime(2026, 1, 1, 12, tzinfo=UTC),
        stop_when_empty=False,
    ) == HOT_INTERVAL_MINUTES


def test_schedule_uses_tier_stagger_and_clamps_to_anchor():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert _next_update_interval(now, None, "entry-1") is None
    assert _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1") == timedelta(
        minutes=MID_INTERVAL_MINUTES + _stagger_minutes("entry-1")
    )
    late = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    assert late + _next_update_interval(late, MID_INTERVAL_MINUTES, "entry-1") == datetime(
        2026, 1, 2, tzinfo=UTC
    )
