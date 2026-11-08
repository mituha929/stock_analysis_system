"""Opt-in complete-period policy for guided AI scans. No network or writes."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from app.services.backfill_policy import period_start, aggregate_period_end
from app.services.taiwan_trading_calendar import (
    latest_trading_day_on_or_before, latest_completed_trading_week, latest_completed_trading_month)

TAIPEI=timezone(timedelta(hours=8))


class ScreenerQuality:
    def __init__(self, interval, now=None):
        if interval not in {"day","week","month"}: raise ValueError("AI 選股只支援日／週／月 K")
        self.interval=interval
        self.now=(now or datetime.now(timezone.utc)).astimezone(TAIPEI)
        # Match the existing conservative 14:00 completion boundary, explicitly
        # in Taipei rather than relying on the execution host's timezone.
        day=self.now.date()-timedelta(days=self.now.hour<14)
        self.end={"day":latest_trading_day_on_or_before,"week":latest_completed_trading_week,
                  "month":latest_completed_trading_month}[interval](day)
        self.label=period_start(interval,self.end)
        self.periods={}; self.halts=self._halt_evidence()

    def _halt_evidence(self):
        # Read only an already downloaded cache. Never refresh it during scan.
        root=Path(__file__).resolve().parents[2]
        path=Path(os.getenv("CAPITAL_SUSPENSION_CACHE_FILE",str(root/"data/trading_suspensions.json")))
        if not path.is_absolute(): path=root/path
        result={}
        try:
            if path.stat().st_size>4*1024*1024: return result
            data=json.loads(path.read_text(encoding="utf-8"))
            for source in data.get("sources",{}).values():
                if source.get("error") or source.get("fetched_on")!=self.now.date().isoformat(): continue
                for row in source.get("records",[]):
                    if row.get("observed_on")!=self.now.date().isoformat(): continue
                    if row["start"]<=self.end.isoformat()<=row["end"]:
                        result[(int(row["market"]),row["symbol"])]=row["source"]
        except (OSError,ValueError,TypeError,KeyError):
            return {}
        return result

    def complete(self, label, fetched_at):
        day=label.date()
        if self.interval=="day":
            if day>self.end: return False
            # A non-trading-day daily row is not a valid replacement for a bar.
            if latest_trading_day_on_or_before(day)!=day: return False
            end=day
        else:
            first=period_start(self.interval,day)
            if first not in self.periods: self.periods[first]=aggregate_period_end(self.interval,day)
            end=self.periods[first]
            if end>self.end: return False
        if fetched_at is None: return False
        written=fetched_at.replace(tzinfo=timezone.utc) if fetched_at.tzinfo is None else fetched_at.astimezone(timezone.utc)
        return written>=datetime(end.year,end.month,end.day,6,tzinfo=timezone.utc)

    def assess(self, candles, metadata, symbol, markets):
        latest=candles[-1].timestamp if candles else None
        info=dict(latest_candle_at=latest,latest_written_at=metadata.get("latest_written_at"),
            expected_period_end=self.end.isoformat(),expected_period_label=self.label.isoformat())
        if not metadata.get("raw_count"):
            return "missing_data",["尚無所選週期的歷史資料"],info
        if not candles:
            return "unverified_period",["沒有可確認已收盤的完整期間；不以寫入時間代替 K 線日期"],info
        if period_start(self.interval,latest.date())!=self.label:
            evidence=[self.halts.get(({5:0,6:1}.get(m,m),symbol)) for m in markets]
            if any(evidence):
                info["instrument_status"]="fresh_halt_evidence"
                note="本地官方快取有期末日停牌紀錄；不代表整段期間都停牌，仍不列入正常符合名單。"
            else:
                info["instrument_status"]="unknown"
                note="可能為缺資料、無成交或停牌；沒有足夠證據判定，不能推定仍正常交易。"
            return "stale_data",[f"最新完整 K 線未到應有期間 {self.label}。",note],info
        return None,[],info

    def snapshot(self):
        return dict(policy="complete_period_v1",checked_at=self.now.isoformat(),
            expected_period_end=self.end.isoformat(),expected_period_label=self.label.isoformat(),
            note="只使用已確認完整且最新期間的資料；不代表已證明所有歷史 K 線連續無缺口。")

    def describe(self,decision):
        from app.ai.rule_preview import explain_rule
        def display(value):
            if value is None: return "資料不足"
            if isinstance(value,bool): return "是" if value else "否"
            if isinstance(value,float): return f"{value:.4f}".rstrip("0").rstrip(".")
            if isinstance(value,list): return "、".join(display(v) for v in value)
            return str(value)
        for detail in decision["details"]:
            label=explain_rule({k:detail[k] for k in ("field","operator","value")},self.interval)["label"]
            detail["reason"]=label+f"；本次值 {display(detail['actual'])}；比較值 {display(detail['expected'])}；"+{
                "matched":"符合","not_matched":"不符合","insufficient_data":"資料不足，無法判定"}[detail["status"]]
        decision["reasons"]=[d["reason"] for d in decision["details"] if d["matched"] is True]
