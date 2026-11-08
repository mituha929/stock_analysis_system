"""Guided AI previews: ASGI only, fake model/repository, no application startup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta,timezone
import json
import unittest
from unittest.mock import Mock,patch
from pydantic import ValidationError
from app.ai.preview_schemas import PreviewRequest,PreviewSettings,ExecuteRequest,ReviseRequest
from app.ai.preview_store import PreviewStore
from app.ai.preview_service import make_preview,confirmed_data,revise_preview
from app.ai.rule_preview import explain_rule,rule_issues,input_issues
from app.ai.schemas import ParseResponse
from app.ai.ollama_client import AIError
from app.api.screener import scan_lock
from app.schemas.models import Candle
from app.services.screener_quality import ScreenerQuality
from app.services.screener_service import screen
from app.services.screener_repository import ScreenerRepository
from app.services.mysql_instrument_repository import MySQLSettings,MySQLRepositoryError
from test_rules import request

NOW=datetime(2026,10,30,7,tzinfo=timezone.utc)


def leaf(field="price",op=">",value=0): return dict(field=field,operator=op,value=value)
def parsed(rule=None,**kwargs):
    return ParseResponse(rule=rule or leaf(),assumptions=[],unresolved_conditions=[],
        original_text="股價高於0",history_interval="day",model="fake",prompt_version="test",status="draft",elapsed_ms=1,**kwargs)
def config(**kwargs): return PreviewSettings(**kwargs)
def execute_payload(preview,**kwargs):
    return ExecuteRequest.model_validate(dict(preview_id=preview["preview_id"],revision=preview["revision"],
        text=preview["original_text"],settings=preview["settings"],confirmed=True,assumptions_confirmed=True,**kwargs))


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.now=0.; self.store=PreviewStore(clock=lambda:self.now)
        self.enterContext(patch("app.services.screener_quality.ScreenerQuality._halt_evidence",return_value={}))
        self.enterContext(patch("app.ai.preview_service.ScreenerQuality",side_effect=lambda i:ScreenerQuality(i,NOW)))

    def preview(self,rule=None,settings=None):
        return make_preview("股價高於0",settings or config(),parsed(rule),store=self.store)

    def test_preview_never_executes_and_contains_nested_explanation(self):
        rule=dict(logic="AND",rules=[leaf("price",">",{"field":"ma20"}),dict(logic="OR",rules=[leaf("rsi14","<=",70),leaf("risk_score","<=",60)])])
        p=self.preview(rule)
        self.assertEqual(p["explanation"]["label"],"全部符合（AND）")
        self.assertEqual(p["explanation"]["children"][1]["label"],"任一符合（OR）")
        self.assertIn("不超過",p["explanation"]["children"][1]["children"][0]["label"])
        self.assertTrue(p["requires_confirmation"])
        self.assertIn("最近20根週 K",explain_rule(leaf("ma20",">",0),"week")["label"])

    def test_each_registered_field_has_readable_label(self):
        from app.rules.fields import FIELDS
        from app.ai.rule_preview import field_label
        for name in FIELDS:
            self.assertNotEqual(field_label(name,"day"),name,name)

    def test_unknown_profit_and_unsupported_mixed_intent_block_before_model(self):
        for text in ("會賺錢的股票","穩賺","下個月一定會漲","股價高於100且財報好","本金虧10%賣出一半","把剛才條件放寬一點"):
            self.assertTrue(input_issues(text,config()),text)
        self.assertFalse(input_issues("股價高於1000",config()))
        self.assertTrue(input_issues("上櫃且股價高於100",config(universe={"markets":[0]})))
        self.assertTrue(input_issues("週線RSI小於70",config()))

    def test_contradiction_and_units_are_not_silently_ignored(self):
        for children in ([leaf("rsi14",">",70),leaf("rsi14","<",30)],
                         [leaf("price",">",100),leaf("price","<=",100)],
                         [leaf("trend","==","bullish"),leaf("trend","==","bearish")],
                         [leaf("rsi14","between",[40,60]),leaf("rsi14",">",60)]):
            self.assertTrue(rule_issues(dict(logic="AND",rules=children)))
        self.assertFalse(rule_issues(dict(logic="OR",rules=[leaf("price",">",100),leaf("price","<",50)])))
        self.assertTrue(rule_issues(leaf("volume",">",1000)))
        self.assertTrue(rule_issues(leaf("price",">",{"field":"volume"})))

    def test_unresolved_and_contradictory_preview_has_no_token(self):
        for rule,issues in ((leaf(),["新聞資料不支援"]),(dict(logic="AND",rules=[leaf("rsi14",">",70),leaf("rsi14","<",30)]),[])):
            p=make_preview("條件",config(),parsed(rule),issues=issues,store=self.store)
            self.assertIsNone(p["preview_id"]); self.assertIsNone(p["rule"])
            self.assertEqual(p["status"],"needs_clarification")
        p=make_preview("股價高於0",config(universe={"kind":"watchlist"}),parsed(),watchlist=[],store=self.store)
        self.assertIsNone(p["rule"])

    def test_single_use_expiry_and_restart(self):
        p=self.preview(); payload=execute_payload(p)
        confirmed_data(payload,self.store)
        with self.assertRaises(AIError): confirmed_data(payload,self.store)
        p=self.preview(); self.now=601
        with self.assertRaises(AIError): confirmed_data(execute_payload(p),self.store)
        with self.assertRaises(AIError): confirmed_data(execute_payload(p),PreviewStore())

    def test_changed_text_and_every_setting_reject_old_confirmation(self):
        p=self.preview()
        for field,value in (("limit",30),("history_bars",100),("timeout_seconds",50),("history_interval","week"),
                            ("universe",{"kind":"symbols","markets":[0],"symbols":["2330"]}),
                            ("sort",[{"field":"symbol","direction":"asc"}])):
            settings={**p["settings"],field:value}
            bad=execute_payload(p).model_copy(update={"settings":PreviewSettings.model_validate(settings)})
            with self.assertRaises(AIError): confirmed_data(bad,self.store)
        with self.assertRaises(AIError): confirmed_data(execute_payload(p).model_copy(update={"text":"新需求"}),self.store)
        confirmed_data(execute_payload(p),self.store)  # Rejected mismatches did not consume original.

    def test_changed_completed_period_requires_new_preview(self):
        p=self.preview()
        with patch("app.ai.preview_service.ScreenerQuality",side_effect=lambda i:ScreenerQuality(i,NOW+timedelta(days=3))):
            with self.assertRaises(AIError): confirmed_data(execute_payload(p),self.store)

    def test_revoke_edit_and_revise_issue_new_token(self):
        p=self.preview(leaf("rsi14","<=",70)); self.store.revoke_for_edit(p["preview_id"])
        with self.assertRaises(AIError): confirmed_data(execute_payload(p),self.store)
        new=revise_preview(ReviseRequest(preview_id=p["preview_id"],revision=p["revision"],rule=leaf("rsi14","<=",60)),self.store)
        self.assertEqual(new["assumptions"][0]["source_text"],"使用者修改門檻")
        self.assertNotEqual(p["preview_id"],new["preview_id"])
        self.assertIn("不超過 60",new["explanation"]["label"])
        confirmed_data(execute_payload(new),self.store)

    def test_concurrent_claims_only_one_succeeds_and_capacity_is_bounded(self):
        p=self.preview()
        def claim():
            try: confirmed_data(execute_payload(p),self.store); return True
            except AIError: return False
        with ThreadPoolExecutor(max_workers=2) as pool: self.assertEqual(sum(pool.map(lambda _:claim(),range(2))),1)
        self.store=PreviewStore(capacity=2)
        a=self.preview(); self.preview(); self.preview()
        self.assertEqual(len(self.store.entries),2)
        with self.assertRaises(AIError): self.store.read(a["preview_id"],a["revision"])

    def test_confirmation_and_contract_validation(self):
        p=self.preview(); body=execute_payload(p).model_dump()
        for field in ("confirmed","assumptions_confirmed"):
            for bad in (False,1,"true",None):
                with self.assertRaises(ValidationError): ExecuteRequest.model_validate({**body,field:bad})
        for body in ({"text":"條件","history_interval":"1m"},{"text":"條件","settings":{"history_interval":"week"}}):
            with self.assertRaises(ValidationError): PreviewRequest.model_validate(body)


def history(end,count=65):
    from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before
    days=[]
    while len(days)<count:
        end=latest_trading_day_on_or_before(end);days.append(end);end-=timedelta(days=1)
    return [Candle(timestamp=datetime.combine(d,datetime.min.time()),open=100,high=120,low=90,close=110,volume=1000) for d in reversed(days)]


class QualityRepo:
    def __init__(self):
        self.rows=[dict(market_no=0,symbol=s,name=s,category_no=1,category_name="測試") for s in ("FRESH","STALE","PARTIAL","EMPTY")]
        self.data={"FRESH":history(date(2026,10,30)),"STALE":history(date(2026,10,29)),"PARTIAL":history(date(2026,10,30))}
    def instruments(self,markets,categories,table,symbols=None): return [r for r in self.rows if symbols is None or r["symbol"] in symbols]
    def candles(self,*args): raise AssertionError("AI must check complete/fresh data BEFORE prefilter")
    def confirmed_candles(self,symbol,interval,table,limit,policy):
        raw=self.data.get(symbol,[])
        written=datetime(2026,10,30,4 if symbol=="PARTIAL" else 7,tzinfo=timezone.utc)
        selected=[c for c in raw if policy.complete(c.timestamp,written)][-limit:]
        return selected,dict(raw_count=len(raw),latest_written_at=written)


class QualityTests(unittest.TestCase):
    def setUp(self): self.enterContext(patch.object(ScreenerQuality,"_halt_evidence",return_value={}))
    def test_fresh_stale_partial_and_missing_are_separate_before_prefilter(self):
        result=screen(config().screener(leaf()),QualityRepo(),quality_policy=ScreenerQuality("day",NOW))
        self.assertEqual(result["matched_count"],1)
        self.assertEqual(result["excluded_counts"]["stale_data"],2)
        self.assertEqual(result["excluded_counts"]["missing_data"],1)
        self.assertTrue(result["complete"])
        self.assertIn("收盤價",result["items"][0]["reasons"][0])
        self.assertEqual(result["data_quality"]["expected_period_end"],"2026-10-30")

    def test_before_close_weekend_holiday_and_month_end(self):
        self.assertEqual(ScreenerQuality("day",datetime(2026,10,30,5,tzinfo=timezone.utc)).end,date(2026,10,29))
        self.assertEqual(ScreenerQuality("day",datetime(2026,11,1,8,tzinfo=timezone.utc)).end,date(2026,10,30))
        self.assertEqual(ScreenerQuality("day",datetime(2026,10,9,8,tzinfo=timezone.utc)).end,date(2026,10,8))
        self.assertEqual(ScreenerQuality("week",NOW).end,date(2026,10,30))
        self.assertEqual(ScreenerQuality("month",NOW).end,date(2026,10,30))

    def test_week_month_per_row_completion_not_latest_global_write_time(self):
        for interval,label in (("week",datetime(2026,10,26)),("month",datetime(2026,10,1))):
            p=ScreenerQuality(interval,NOW)
            self.assertFalse(p.complete(label,datetime(2026,10,29,8,tzinfo=timezone.utc)))
            self.assertTrue(p.complete(label,NOW))
            self.assertFalse(p.complete(label,None))
            self.assertFalse(p.complete(datetime(2026,11,2),NOW+timedelta(days=10)))

    def test_trusted_halt_note_does_not_turn_stale_data_into_match(self):
        p=ScreenerQuality("day",NOW);p.halts[(0,"STALE")]="official-cache"
        result=screen(config().screener(leaf()),QualityRepo(),quality_policy=p)
        stale=next(x for x in result["excluded"] if x["symbol"]=="STALE")
        self.assertEqual(stale["instrument_status"],"fresh_halt_evidence")
        self.assertEqual(stale["status"],"stale_data")

    def test_limit_and_partial_are_distinct(self):
        repo=QualityRepo();repo.rows=repo.rows[:1]*2
        # Use unique symbols with valid data for both.
        repo.rows[1]={**repo.rows[0],"symbol":"FRESH2"};repo.data["FRESH2"]=repo.data["FRESH"]
        result=screen(config(limit=1).screener(leaf()),repo,quality_policy=ScreenerQuality("day",NOW))
        self.assertTrue(result["complete"]);self.assertTrue(result["truncated"])
        ticks=iter([0,0,2,2])
        result=screen(config(timeout_seconds=1).screener(leaf()),repo,clock=lambda:next(ticks),quality_policy=ScreenerQuality("day",NOW))
        self.assertFalse(result["complete"]);self.assertEqual(result["remaining_symbols"],["FRESH2"])

    def test_stream_filters_partial_rows_without_mutating_legacy_reader(self):
        repo=ScreenerRepository(MySQLSettings("unused",3306,"test","test","test"));cur=Mock();repo.connection=Mock();repo.connection.cursor.return_value=cur
        full=(datetime(2026,10,30),100,120,90,110,1000,NOW)
        early=(datetime(2026,10,31),100,120,90,110,1000,NOW)
        cur.fetchmany.side_effect=[[early,full],[],[]]
        rows,metadata=repo.confirmed_candles("FRESH","day","candles",20,ScreenerQuality("day",NOW))
        self.assertEqual(len(rows),1);self.assertEqual(metadata["excluded_unverified_bars"],1)
        cur.close.assert_called_once()
        self.assertTrue(cur.execute.call_args.args[0].startswith("SELECT"))


class GuidedApiTests(unittest.TestCase):
    def setUp(self):
        self.store=PreviewStore()
        for name in ("app.api.ai_rules.preview_store","app.ai.preview_service.preview_store"):
            self.enterContext(patch(name,self.store))
        self.enterContext(patch("app.services.screener_quality.ScreenerQuality._halt_evidence",return_value={}))
        for name in ("app.api.ai_rules.ScreenerQuality","app.ai.preview_service.ScreenerQuality"):
            self.enterContext(patch(name,side_effect=lambda i:ScreenerQuality(i,NOW)))
        self.parser=self.enterContext(patch("app.api.ai_rules.parse_rules",return_value=parsed()))
        self.repo=QualityRepo();self.repository=self.enterContext(patch("app.api.ai_rules.ScreenerRepository"))
        self.repository.return_value.__enter__.return_value=self.repo

    def post(self,path,body): return asyncio.run(request("POST","/api/v1/ai/rules"+path,body))
    def preview(self):
        code,p=self.post("/preview",{"text":"股價高於0"});self.assertEqual(code,200);return p
    def execute(self,p): return self.post("/execute",execute_payload(p).model_dump(mode="json"))

    def test_preview_then_confirmed_scan_and_duplicate_rejection(self):
        p=self.preview();self.repository.assert_not_called()
        code,result=self.execute(p)
        self.assertEqual(code,200);self.assertEqual(result["matched_count"],1)
        self.assertEqual(result["confirmed_preview"]["revision"],p["revision"])
        self.assertEqual(self.execute(p)[0],409)
        self.parser.assert_called_once()

    def test_guards_do_not_call_model_or_database(self):
        for text in ("找會賺錢的股票","RSI小於70而且新聞好","上櫃股價高於100"):
            settings={"universe":{"markets":[0]}}
            code,p=self.post("/preview",{"text":text,"settings":settings})
            self.assertEqual(code,200);self.assertEqual(p["status"],"needs_clarification")
            self.assertIsNone(p["preview_id"])
        self.parser.assert_not_called();self.repository.assert_not_called()

    def test_busy_does_not_consume_token_database_failure_does(self):
        p=self.preview()
        with scan_lock: self.assertEqual(self.execute(p)[0],409)
        self.repository.return_value.__enter__.side_effect=MySQLRepositoryError("fake offline")
        self.assertEqual(self.execute(p)[0],503)
        self.assertEqual(self.execute(p)[0],409)

    def test_model_failure_never_runs_empty_rule(self):
        from fastapi import HTTPException
        self.parser.side_effect=HTTPException(504,{"code":"ollama_timeout","message":"fake timeout"})
        self.assertEqual(self.post("/preview",{"text":"股價高於0"})[0],504)
        self.repository.assert_not_called()

    def test_replacement_and_invalidation_make_old_token_unusable(self):
        p=self.preview()
        self.post("/preview/invalidate",{"preview_id":p["preview_id"]})
        self.assertEqual(self.execute(p)[0],409)
        p=self.preview();self.post("/preview",{"text":"新需求","replaces":p["preview_id"]})
        self.assertEqual(self.execute(p)[0],409)

    def test_json_extra_options_and_minute_timeframes_rejected(self):
        for body in ({"text":"條件","history_interval":"1m"},{"text":"條件","settings":{"prefilter":{"min_volume":100}}}):
            self.assertEqual(self.post("/preview",body)[0],422)
        self.parser.assert_not_called()

