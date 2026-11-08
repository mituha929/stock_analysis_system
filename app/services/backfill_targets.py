"""History goals, NEVER storage limits. Latest-data upserts remain unbounded."""
from __future__ import annotations

import os

DEFAULT_TARGET_BARS = {
    "1m":1200,"5m":1200,"15m":1200,"60m":1200,"day":1200,"week":560,"month":100,
}
DEFAULT_REFRESH_BARS = {
    "1m":1200,"5m":240,"15m":60,"60m":20,"day":10,"week":5,"month":2,
}


def prioritize_day(items, key=lambda item: item):
    """Stable partition only: preserve relative market/non-day order and objects."""
    return sorted(items, key=lambda item: key(item) != "day")


def parse_targets(value):
    targets = dict(DEFAULT_TARGET_BARS)
    if not value.strip(): return targets
    for item in value.split(","):
        if ":" not in item:
            raise ValueError("CAPITAL_DAILY_BACKFILL_TARGETS 必須使用 interval:bars 格式，例如 week:560,month:100")
        interval, raw = (part.strip().lower() for part in item.split(":",1))
        if interval not in targets: raise ValueError(f"不支援的 K 線週期：{interval}")
        targets[interval] = validate_target(raw)
    return targets


def validate_target(value):
    try: result = int(value)
    except (ValueError,TypeError) as error: raise ValueError("目標根數必須是整數") from error
    if isinstance(value,bool) or not 1 <= result <= 10000:
        raise ValueError("目標根數必須介於 1 到 10000")
    return result


def configured_targets():
    return parse_targets(os.getenv("CAPITAL_DAILY_BACKFILL_TARGETS",""))


def configured_refresh_targets():
    """Daily re-query goals, independent of historical initialization goals."""
    targets = dict(DEFAULT_REFRESH_BARS)
    raw = os.getenv("CAPITAL_DAILY_REFRESH_TARGETS", "").strip()
    if raw:
        for item in raw.split(","):
            if ":" not in item:
                raise ValueError("CAPITAL_DAILY_REFRESH_TARGETS 必須使用 interval:bars 格式")
            interval, value = (part.strip().lower() for part in item.split(":", 1))
            if interval not in targets:
                raise ValueError(f"不支援的日常回抓週期：{interval}")
            targets[interval] = validate_target(value)
    return targets


def target_for(interval, override=None):
    if interval not in DEFAULT_TARGET_BARS: raise ValueError(f"不支援的 K 線週期：{interval}")
    if isinstance(override,dict): return validate_target(override[interval])
    return validate_target(override) if override is not None else configured_targets()[interval]


def refresh_plan_targets(plans):
    """Adopt configured goals on safe restart, preserving deliberate overrides.

    Older saved plans had no origin field: migrate the old blanket-1200 default
    while preserving legacy custom goals. Replan from actual rows/checkpoints;
    never clear candles, checked-through dates, or history cursors here.
    """
    targets = configured_targets()
    for plan in plans:
        interval = plan["interval"]
        source = plan.get("target_source")
        previous = plan.get("target_bars")
        if source is None:
            source = "config" if previous in (None,1200) else "override"
            plan["target_source"] = source
        if source == "config" and previous != targets[interval]:
            plan.update(target_bars=targets[interval],planned_on=None,force=False)
