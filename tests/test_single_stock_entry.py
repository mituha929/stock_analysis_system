import asyncio
from datetime import datetime,timedelta,timezone
import json
from unittest import TestCase
from unittest.mock import Mock,patch

from app.ai.ollama_client import AIError,OllamaConfig
from app.ai.preview_schemas import PreviewRequest,ExecuteRequest,ReviseRequest
from app.ai.preview_store import PreviewStore
from app.ai.preview_service import revise_preview
from app.ai.single_stock import (make_entry_preview,analyze_entry,INTENT,EXIT_INTENT,EXIT_STRATEGY,EXIT_RULE,
                                ADD_INTENT,REDUCE_INTENT,ADD_RULE,REDUCE_RULE)
from app.schemas.models import Candle
from app.services.screener_quality import ScreenerQuality
from app.services.taiwan_trading_calendar import latest_trading_day_on_or_before
from test_rules import request

NOW=datetime(2026,10,30,7,tzinfo=timezone.utc)


def bars(count=90,stale=False):
    dates=[];day=NOW.date()-timedelta(days=stale)
    while len(dates)<count:
        day=latest_trading_day_on_or_before(day);dates.append(day);day-=timedelta(days=1)
    return [Candle(timestamp=datetime.combine(d,datetime.min.time()),open=100+i,
        high=102+i,low=99+i,close=101+i,volume=3000 if i==count-1 else 1000)
        for i,d in enumerate(reversed(dates))]


class EntryTests(TestCase):
    def setUp(self):
        self.store=PreviewStore()
        self.enterContext(patch.object(ScreenerQuality,"_halt_evidence",return_value={}))
        for path in ("app.ai.preview_service.ScreenerQuality","app.ai.single_stock.ScreenerQuality"):
            self.enterContext(patch(path,side_effect=lambda interval:ScreenerQuality(interval,NOW)))
        self.repo=Mock()
        self.repo.__enter__=Mock(return_value=self.repo);self.repo.__exit__=Mock(return_value=None)
        self.repo.instruments.return_value=[dict(market_no=0,symbol="2454",name="測試商品")]
        self.repo.confirmed_candles.return_value=(bars(),dict(raw_count=90,latest_written_at=NOW))
        self.factory=Mock(return_value=self.repo)
        self.client=Mock(config=OllamaConfig())
        self.client.chat.side_effect=lambda messages,schema:json.dumps(dict(
            assessment=json.loads(messages[-1]["content"])["assessment"],
            focus_evidence_ids=["condition_2","atr14"]))

    def preview(self,text="我想要確認2454是否可以買進",**settings):
        return make_entry_preview(PreviewRequest(text=text,settings=settings),self.store)

    def execute(self,p):
        payload=ExecuteRequest(preview_id=p["preview_id"],revision=p["revision"],text=p["original_text"],
            settings=p["settings"],confirmed=True,assumptions_confirmed=True)
        return analyze_entry(payload,self.client,self.factory,self.store)

    def test_preview_narrows_to_one_symbol_without_io(self):
        p=self.preview()
        self.assertEqual(p["intent"],INTENT)
        self.assertEqual(p["resolved_settings"]["universe"]["symbols"],["2454"])
        self.assertEqual(p["settings"]["universe"]["kind"],"markets")
        self.assertTrue(p["requires_confirmation"])
        self.assertEqual(len(p["rule"]["rules"]),3)
        self.factory.assert_not_called();self.client.chat.assert_not_called()

    def test_preserves_leading_zero_and_handles_explicit_scope(self):
        p=self.preview("我想確認0050是否可以買進")
        self.assertEqual(p["entry_analysis"]["symbol"],"0050")
        p=self.preview("現在可以買進嗎",universe=dict(kind="symbols",symbols=["2454"]))
        self.assertEqual(p["entry_analysis"]["symbol"],"2454")

    def test_sell_preview_has_independent_strategy_and_no_io(self):
        for text in ("我想要確認2454是否可以賣出","2454可不可以出場","我持有2454，現在想賣掉"):
            with self.subTest(text=text):
                p=self.preview(text)
                self.assertEqual(p["intent"],EXIT_INTENT)
                self.assertEqual(p["exit_analysis"]["strategy"],EXIT_STRATEGY)
                self.assertEqual(p["stock_analysis"]["symbol"],"2454")
                self.assertEqual(p["rule"],EXIT_RULE)
                self.assertTrue(p["preview_id"])
        p=self.preview("００５０是否可以賣出")
        self.assertEqual(p["stock_analysis"]["symbol"],"0050")
        self.factory.assert_not_called();self.client.chat.assert_not_called()

    def test_add_reduce_previews_and_user_explanations(self):
        for text,intent,rule in (("2454是否可以加碼",ADD_INTENT,ADD_RULE),
                ("我想要確認2454是否該增持",ADD_INTENT,ADD_RULE),
                ("2454是否需要減碼",REDUCE_INTENT,REDUCE_RULE),
                ("請幫我判斷2454能否減碼",REDUCE_INTENT,REDUCE_RULE),
                ("我持有2454要不要減碼賣出",REDUCE_INTENT,REDUCE_RULE),
                ("2454可以加碼買進嗎",ADD_INTENT,ADD_RULE)):
            with self.subTest(text=text):
                p=self.preview(text)
                self.assertEqual(p["intent"],intent)
                self.assertEqual(p["rule"],rule)
                self.assertTrue(p["preview_id"],p["unresolved_conditions"])
                meaning=p["stock_analysis"]["strategy"]["user_explanation"]
                self.assertEqual(set(meaning),{"purpose","when_met","when_not_met"})
                self.assertIn("不計算",p["stock_analysis"]["strategy"]["note"])
        self.factory.assert_not_called();self.client.chat.assert_not_called()

    def test_mixed_or_quantity_requests_are_not_treated_as_bare_add_reduce(self):
        for text in ("2454加碼還是減碼","2454加減碼","2454可以加碼或賣出嗎", "2454加碼20%",
                     "2454減碼一半","2454減碼50%","2454減碼，我的持股成本100元"):
            with self.subTest(text=text):
                p=self.preview(text)
                self.assertEqual(p["status"],"needs_clarification")
                self.assertIsNone(p["preview_id"])

    def test_add_requires_new_reclaim_and_is_not_the_same_as_entry(self):
        buy=self.execute(self.preview())
        add=self.execute(self.preview("2454是否可以加碼"))
        self.assertEqual(buy["assessment"],"conditions_met")
        self.assertEqual(add["assessment"],"conditions_not_met")
        rows=bars();rows[-2]=rows[-2].model_copy(update=dict(open=170,high=172,low=168,close=170))
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        add=self.execute(self.preview("2454是否可以加碼"))
        self.assertEqual(add["assessment"],"conditions_met")
        self.assertTrue(all(f["status"]=="matched" for f in add["facts"][:3]))
        self.assertIn("未確認",add["strategy"]["note"])

    def test_reduce_can_trigger_without_exit_and_has_its_own_explanation(self):
        rows=bars()
        for index,price in ((-2,170),(-1,169)):
            rows[index]=rows[index].model_copy(update=dict(open=price,high=price+2,low=price-2,close=price))
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        reduce=self.execute(self.preview("2454是否可以減碼"))
        sell=self.execute(self.preview("2454是否可以賣出"))
        self.assertEqual(reduce["assessment"],"conditions_met")
        self.assertEqual(sell["assessment"],"conditions_not_met")
        self.assertEqual(reduce["action"],"減碼")
        self.assertIn("不代表需要全部賣出",reduce["strategy"]["user_explanation"]["when_met"])

    def test_add_and_reduce_do_not_invert_each_other(self):
        add=self.execute(self.preview("2454是否可以加碼"))
        reduce=self.execute(self.preview("2454是否可以減碼"))
        self.assertEqual(add["assessment"],"conditions_not_met")
        self.assertEqual(reduce["assessment"],"conditions_not_met")

    def test_mixed_buy_sell_and_personal_exits_require_clarification(self):
        for text in ("2454是否可以買進或賣出","2454買賣嗎","2454是否可以進出場", "2454賣出一半",
                     "2454是否可以賣出，我的成本100元","2454是否可以賣出，停損5%", "2454是否可以賣出，RSI高於70"):
            with self.subTest(text=text):
                p=self.preview(text)
                self.assertEqual(p["status"],"needs_clarification")
                self.assertIsNone(p["preview_id"])

    def test_not_buying_is_not_automatically_selling(self):
        rows=bars();rows[-1]=rows[-1].model_copy(update=dict(volume=1))
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        buy=self.execute(self.preview())
        sell=self.execute(self.preview("2454是否可以賣出"))
        self.assertEqual(buy["assessment"],"conditions_not_met")
        self.assertEqual(sell["assessment"],"conditions_not_met")
        self.assertNotEqual(buy["strategy"]["id"],sell["strategy"]["id"])
        self.assertEqual(sell["ai"]["status"],"explained")
        self.assertIn("未觸發",sell["summary"])
        model_input=json.loads(self.client.chat.call_args.args[0][-1]["content"])
        self.assertEqual(model_input["strategy"]["id"],EXIT_STRATEGY["id"])

    def test_sell_cross_below_uses_previous_bar_and_or_logic(self):
        rows=bars();rows[-1]=rows[-1].model_copy(update=dict(open=101,high=102,low=98,close=100))
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        result=self.execute(self.preview("2454是否可以賣出"))
        self.assertEqual(result["assessment"],"conditions_met")
        self.assertEqual(result["action"],"出場")
        self.assertEqual(result["facts"][0]["status"],"matched")
        self.assertEqual(result["facts"][2]["status"],"not_matched")

    def test_sustained_weakness_is_separate_from_a_new_cross(self):
        rows=[c.model_copy(update=dict(open=200-i,high=202-i,low=198-i,close=200-i))
              for i,c in enumerate(bars())]
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        result=self.execute(self.preview("2454是否可以賣出"))
        self.assertEqual(result["assessment"],"conditions_met")
        self.assertEqual(result["facts"][0]["status"],"not_matched")
        self.assertEqual([f["status"] for f in result["facts"][1:3]],["matched","matched"])

    def test_sell_bad_data_or_changed_entry_confirmation_does_not_reach_ai(self):
        self.repo.confirmed_candles.return_value=(bars(stale=True),dict(raw_count=90,latest_written_at=NOW))
        result=self.execute(self.preview("2454是否可以賣出"))
        self.assertEqual(result["assessment"],"insufficient_data")
        self.assertEqual(result["data_status"],"stale_data")
        p=self.preview()
        payload=ExecuteRequest(preview_id=p["preview_id"],revision=p["revision"],text="2454是否可以賣出",
            settings=p["settings"],confirmed=True,assumptions_confirmed=True)
        with self.assertRaises(AIError):analyze_entry(payload,self.client,self.factory,self.store)
        self.client.chat.assert_not_called()

    def test_ambiguity_conflicts_and_extra_requirements_are_not_dropped(self):
        examples=[("2454、2330可以買進嗎",{}),("可以買進嗎",{}),
            ("2454可以買進嗎",dict(universe=dict(kind="symbols",symbols=["2330"]))),
            ("2454可以買進嗎，要短線",{}),("2454可以買進嗎，RSI低於30",{}),
            ("2454可以買進嗎",dict(history_bars=20)),("2454可以買進嗎，還要看財報",{})]
        for text,settings in examples:
            with self.subTest(text=text,settings=settings):
                p=self.preview(text,**settings)
                self.assertEqual(p["status"],"needs_clarification")
                self.assertIsNone(p["preview_id"])
        self.assertIsNone(make_entry_preview(PreviewRequest(text="股價高於MA20"),self.store))

    def test_confirmed_analysis_uses_only_selected_stock_and_closes_db_before_ai(self):
        def explain(messages,schema):
            self.repo.__exit__.assert_called_once()
            self.assertEqual(json.loads(messages[-1]["content"])["symbol"],"2454")
            return json.dumps(dict(assessment="conditions_met",focus_evidence_ids=["condition_1","atr14"]))
        self.client.chat.side_effect=explain
        p=self.preview();r=self.execute(p)
        self.assertEqual(r["assessment"],"conditions_met")
        self.assertEqual(r["ai"]["status"],"explained")
        self.repo.instruments.assert_called_once_with([0,1],[],"candles",["2454"])
        self.assertEqual(self.repo.confirmed_candles.call_args.args[0],"2454")
        with self.assertRaises(AIError):self.execute(p)

    def test_not_met_is_calculated_not_selected_by_model(self):
        rows=bars();rows[-1]=rows[-1].model_copy(update=dict(volume=1))
        self.repo.confirmed_candles.return_value=(rows,dict(raw_count=90,latest_written_at=NOW))
        r=self.execute(self.preview())
        self.assertEqual(r["assessment"],"conditions_not_met")
        self.assertEqual(r["ai"]["status"],"explained")

    def test_bad_ai_output_keeps_real_program_result(self):
        for output in (dict(assessment="conditions_not_met",focus_evidence_ids=["condition_1"]),
                       dict(assessment="conditions_met",focus_evidence_ids=["invented_price"]),
                       dict(assessment="conditions_met",focus_evidence_ids=["condition_1"],price=999999)):
            with self.subTest(output=output):
                self.client.chat.side_effect=None;self.client.chat.return_value=json.dumps(output)
                r=self.execute(self.preview())
                self.assertEqual(r["assessment"],"conditions_met")
                self.assertEqual(r["ai"]["status"],"unavailable")
                self.assertNotEqual(r["price"],999999)

    def test_missing_stale_or_short_history_never_gets_ai_buy_assessment(self):
        for rows,status in (([],"missing_data"),(bars(30),"insufficient_data"),(bars(stale=True),"stale_data")):
            with self.subTest(status=status):
                self.repo.confirmed_candles.return_value=(rows,dict(raw_count=len(rows),latest_written_at=NOW))
                r=self.execute(self.preview())
                self.assertEqual(r["data_status"],status)
                self.assertEqual(r["assessment"],"insufficient_data")
        self.client.chat.assert_not_called()

    def test_unknown_instrument_does_not_expand_scope(self):
        self.repo.instruments.return_value=[]
        r=self.execute(self.preview())
        self.assertEqual(r["data_status"],"outside_universe")
        self.repo.confirmed_candles.assert_not_called();self.client.chat.assert_not_called()

    def test_edits_and_revisions_require_fresh_confirmation(self):
        p=self.preview()
        payload=ExecuteRequest(preview_id=p["preview_id"],revision=p["revision"],text="2330可以買進嗎",
            settings=p["settings"],confirmed=True,assumptions_confirmed=True)
        with self.assertRaises(AIError):analyze_entry(payload,self.client,self.factory,self.store)
        self.factory.assert_not_called()
        with self.assertRaises(AIError):revise_preview(ReviseRequest(preview_id=p["preview_id"],revision=p["revision"],
            rule=dict(field="price",operator=">",value=0)),self.store)
        self.execute(p)

    def test_api_flow_does_not_invoke_general_parser_or_screener(self):
        with patch("app.api.ai_rules.preview_store",self.store), \
             patch("app.ai.single_stock.preview_store",self.store), \
             patch("app.ai.preview_service.preview_store",self.store), \
             patch("app.api.ai_rules.ScreenerRepository",self.factory), \
             patch("app.api.ai_rules.OllamaClient",return_value=self.client), \
             patch("app.api.ai_rules.parse_rules") as parser,patch("app.api.ai_rules.screen") as screen:
            status,p=asyncio.run(request("POST","/api/v1/ai/rules/preview",dict(text="2454可以買進嗎")))
            self.assertEqual(status,200)
            self.factory.assert_not_called();self.client.chat.assert_not_called()
            body=dict(preview_id=p["preview_id"],revision=p["revision"],text=p["original_text"],settings=p["settings"],
                      confirmed=True,assumptions_confirmed=True)
            status,r=asyncio.run(request("POST","/api/v1/ai/rules/execute",body))
            self.assertEqual(status,200,r)
            self.assertEqual(r["intent"],INTENT)
            self.assertEqual(r["symbol"],"2454")
        parser.assert_not_called();screen.assert_not_called()

    def test_sell_api_uses_single_stock_path_after_confirmation(self):
        with patch("app.api.ai_rules.preview_store",self.store), \
             patch("app.ai.single_stock.preview_store",self.store), \
             patch("app.ai.preview_service.preview_store",self.store), \
             patch("app.api.ai_rules.ScreenerRepository",self.factory), \
             patch("app.api.ai_rules.OllamaClient",return_value=self.client), \
             patch("app.api.ai_rules.parse_rules") as parser,patch("app.api.ai_rules.screen") as screen:
            status,p=asyncio.run(request("POST","/api/v1/ai/rules/preview",dict(text="2454 是否可以賣出")))
            self.assertEqual(status,200,p)
            self.assertEqual(p["intent"],EXIT_INTENT)
            self.factory.assert_not_called();self.client.chat.assert_not_called()
            body=dict(preview_id=p["preview_id"],revision=p["revision"],text=p["original_text"],settings=p["settings"],
                      confirmed=True,assumptions_confirmed=True)
            status,r=asyncio.run(request("POST","/api/v1/ai/rules/execute",body))
            self.assertEqual(status,200,r)
            self.assertEqual(r["intent"],EXIT_INTENT)
            self.assertEqual(r["symbol"],"2454")
        parser.assert_not_called();screen.assert_not_called()

    def test_add_reduce_api_routes_to_exact_stock_after_confirmation(self):
        with patch("app.api.ai_rules.preview_store",self.store), \
             patch("app.ai.single_stock.preview_store",self.store), \
             patch("app.ai.preview_service.preview_store",self.store), \
             patch("app.api.ai_rules.ScreenerRepository",self.factory), \
             patch("app.api.ai_rules.OllamaClient",return_value=self.client), \
             patch("app.api.ai_rules.parse_rules") as parser,patch("app.api.ai_rules.screen") as screen:
            for word,intent in (("加碼",ADD_INTENT),("減碼",REDUCE_INTENT)):
                with self.subTest(word=word):
                    status,p=asyncio.run(request("POST","/api/v1/ai/rules/preview",dict(text=f"2454是否可以{word}")))
                    self.assertEqual(status,200,p)
                    self.assertEqual(p["intent"],intent)
                    body=dict(preview_id=p["preview_id"],revision=p["revision"],text=p["original_text"],settings=p["settings"],
                              confirmed=True,assumptions_confirmed=True)
                    status,r=asyncio.run(request("POST","/api/v1/ai/rules/execute",body))
                    self.assertEqual(status,200,r)
                    self.assertEqual(r["intent"],intent)
                    self.assertEqual(r["symbol"],"2454")
        parser.assert_not_called();screen.assert_not_called()
