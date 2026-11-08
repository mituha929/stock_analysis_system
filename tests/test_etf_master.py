import asyncio
from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services.etf_master_sources import TPEX_ETF_FIELDS, TPEX_ETF_TOP, TWSE_ETF_FIELDS, parse_etf_snapshot
from app.services.stock_master_sources import ETF_SOURCES, SOURCES, StockSourceError, fetch_snapshots, read_official, validate_code, validate_snapshots
from app.services.stock_master_repository import DATA_COLUMNS, StockSyncError, _ensure_storage
from test_stock_master import NOW, TODAY, api_get, snapshots, source_row


def twse_payload(rows=None):
    return json.dumps({"stat":"OK","fields":TWSE_ETF_FIELDS,"data": rows if rows is not None else [["2003.06.30","0050","元大台灣50","元大投信","臺灣50指數"]]})


def tpex_row(code="00679B", listing="20170117", name="元大美債20年"):
    row = dict.fromkeys(TPEX_ETF_FIELDS, "")
    row.update(stockNo=code, listingDate=listing, stockName=name)
    return row


def tpex_payload(rows=None):
    value = dict.fromkeys(TPEX_ETF_TOP, [])
    value.update(status="success", data=rows if rows is not None else [tpex_row()])
    return json.dumps(value)


class ETFSourceTests(unittest.TestCase):
    def test_passive_active_leveraged_and_bond_codes(self):
        for code in ("0050","006203","00631L","00632R","00635U","00625K","00400A","00980D","00679B"):
            self.assertEqual(validate_code(code),code)
        for code in ("abc123","00631l","00631LL","TSEA","0050'",1234):
            with self.assertRaises(StockSourceError):
                validate_code(code)

    def test_twse_date_not_fund_establishment_date(self):
        snapshot=parse_etf_snapshot(twse_payload(),"TWSE",TODAY,minimum=1)
        record=snapshot.records[0]
        self.assertEqual(record.listing_date,date(2003,6,30))
        self.assertEqual((record.security_type,record.industry,record.issuer_id),("ETF",None,None))
        self.assertEqual((snapshot.source_date_kind,snapshot.source_date),("retrieval_date",TODAY))
        self.assertEqual(snapshot.source_url,ETF_SOURCES["TWSE"])

    def test_tpex_upcoming_and_missing_dates(self):
        rows=[tpex_row(),tpex_row("00980D",listing="20261029"),tpex_row("00981D",listing=None)]
        snapshot=parse_etf_snapshot(tpex_payload(rows),"TPEx",TODAY,minimum=1)
        self.assertEqual(snapshot.records[0].listing_date,date(2017,1,17))
        self.assertEqual(snapshot.records[1].listing_date,date(2026,10,29))
        self.assertIsNone(snapshot.records[2].listing_date)

    def test_currency_dates_match_labels_even_when_reordered(self):
        row=["2011.09.26(新臺幣)<br>2016.08.08(人民幣)","00625K(人民幣)<br>006205(新臺幣)",
             "富邦上証(新臺幣)<br>富邦上証+R(人民幣)","富邦投信","上証180指數"]
        records=parse_etf_snapshot(twse_payload([row]),"TWSE",TODAY,minimum=1).records
        values={r.code:r.listing_date for r in records}
        self.assertEqual(values,{"006205":date(2011,9,26),"00625K":date(2016,8,8)})

    def test_currency_missing_label_or_count_mismatch_rejected(self):
        base=["2011.09.26(新臺幣)<br>2016.08.08(人民幣)","006205(新臺幣)<br>00625K(人民幣)",
              "富邦上証(新臺幣)<br>富邦上証+R(人民幣)","富邦投信","指數"]
        for index,value in ((0,"2011.09.26"),(0,"2011.09.26<br>2016.08.08"),
                            (1,"006205(新臺幣)<br>00625K(美元)"),(2,"富邦上証(新臺幣)<br>富邦上証(新臺幣)")):
            row=base.copy();row[index]=value
            with self.assertRaises(StockSourceError):
                parse_etf_snapshot(twse_payload([row]),"TWSE",TODAY,minimum=1)

    def test_unknown_markup_not_executed_or_guessed(self):
        payload=twse_payload([["<script>x()</script>","0050","名稱","發行人",None]])
        with self.assertRaises(StockSourceError):
            parse_etf_snapshot(payload,"TWSE",TODAY,minimum=1)

    def test_active_funds_need_no_benchmark_index(self):
        snapshot=parse_etf_snapshot(twse_payload([["2026.04.09","00400A","主動國泰動能高息","國泰投信",None]]),"TWSE",TODAY,minimum=1)
        self.assertEqual(snapshot.records[0].code,"00400A")

    def test_empty_lists_failure_status_and_drift_fail_closed(self):
        payloads=[twse_payload([]),json.dumps({"stat":"FAIL","fields":TWSE_ETF_FIELDS,"data":[]}),
                  json.dumps({"stat":"OK","fields":TWSE_ETF_FIELDS+["new"],"data":[[]]}),
                  twse_payload([["2003.06.30","0050","名稱","發行人"]])]
        for payload in payloads:
            with self.assertRaises(StockSourceError):
                parse_etf_snapshot(payload,"TWSE",TODAY,minimum=1)
        data=json.loads(tpex_payload());data["data"][0]["new_field"]=""
        with self.assertRaises(StockSourceError):
            parse_etf_snapshot(json.dumps(data),"TPEx",TODAY,minimum=1)

    def test_duplicate_code_invalid_date_not_repaired(self):
        for payload in (tpex_payload([tpex_row(),tpex_row()]),tpex_payload([tpex_row(listing="20260230")])):
            with self.assertRaises(StockSourceError):
                parse_etf_snapshot(payload,"TPEx",TODAY,minimum=1)

    def test_empty_date_only_retained_as_unknown_if_other_valid_rows(self):
        data=[["2003.06.30","0050","名稱","發行人",None],[None,"0051","名稱二","發行人",None]]
        self.assertIsNone(parse_etf_snapshot(twse_payload(data),"TWSE",TODAY,minimum=1).records[1].listing_date)
        with self.assertRaises(StockSourceError):
            parse_etf_snapshot(twse_payload(data[1:]),"TWSE",TODAY,minimum=1)

    def test_full_fetch_reads_four_sources_no_hardcoded_quotes(self):
        reader=MagicMock(side_effect=[json.dumps([source_row()]),json.dumps([source_row("TPEx","6488","20150925")]),twse_payload(),tpex_payload()])
        items=fetch_snapshots(reader=reader,now=NOW,minimum=1,etf_minimum=1)
        self.assertEqual(len(items),4)
        self.assertEqual([call.args[0] for call in reader.call_args_list],list(SOURCES.values())+list(ETF_SOURCES.values()))
        self.assertEqual({s.source_key for s in items},{"TWSE","TPEx","TWSE:ETF","TPEx:ETF"})

    def test_partial_etf_pair_or_type_conflict_rejected(self):
        company=snapshots()
        etf=parse_etf_snapshot(twse_payload(),"TWSE",TODAY,minimum=1)
        with self.assertRaises(StockSourceError):validate_snapshots(company+[etf],TODAY)
        full=company+[etf,parse_etf_snapshot(tpex_payload(),"TPEx",TODAY,minimum=1)]
        validate_snapshots(full,TODAY)
        bad=replace(etf,records=(replace(etf.records[0],security_type="STOCK"),))
        with self.assertRaises(StockSourceError):validate_snapshots(company+[bad,full[-1]],TODAY)

    def test_tpex_empty_post_is_public_read_query(self):
        response=MagicMock();response.geturl.return_value=ETF_SOURCES["TPEx"];response.read.return_value=tpex_payload().encode()
        with patch("app.services.stock_master_sources.urlopen") as opened:
            opened.return_value.__enter__.return_value=response
            read_official(ETF_SOURCES["TPEx"])
        request=opened.call_args.args[0]
        self.assertEqual((request.get_method(),request.data),("POST",b""))


class ETFMigrationTests(unittest.TestCase):
    def cursor(self,legacy=True,foreign=False):
        cursor=MagicMock()
        def execute(sql,params=None):
            if "information_schema.columns" in sql:
                table=params[0]
                if table in {"stock_master","stock_listing_plans"}:
                    cols=set(DATA_COLUMNS)-({"security_type","source_date_kind"} if legacy else set())
                    if foreign:cols.add("unrelated")
                    cursor.fetchall.return_value=[(c,) for c in cols]
                else:cursor.fetchall.return_value=[]
            elif "SELECT ENGINE" in sql:cursor.fetchone.return_value=("InnoDB",)
            elif "information_schema.statistics" in sql:cursor.fetchall.return_value=[("code",),("market",)]
        cursor.execute.side_effect=execute
        return cursor

    def test_legacy_migration_adds_only_two_columns_to_each_profile_table(self):
        cursor=self.cursor()
        _ensure_storage(cursor)
        alters=[call.args[0] for call in cursor.execute.call_args_list if "ALTER TABLE" in call.args[0]]
        self.assertEqual(len(alters),4)
        self.assertTrue(all("ADD COLUMN" in sql and "DROP" not in sql for sql in alters))

    def test_current_schema_does_not_repeat_migration(self):
        cursor=self.cursor(legacy=False)
        _ensure_storage(cursor)
        self.assertFalse(any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list))

    def test_foreign_columns_rejected_before_any_alter(self):
        cursor=self.cursor(foreign=True)
        with self.assertRaises(StockSyncError):_ensure_storage(cursor)
        self.assertFalse(any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list))


class ETFAPITests(unittest.TestCase):
    def test_alpha_code_routes_to_same_mysql_profile_repository(self):
        from app.main import app
        from app.api.stock_profiles import repository
        repo=MagicMock();repo.profile.return_value={"code":"00631L","security_type":"ETF","listing_date":"2014-10-31"}
        app.dependency_overrides[repository]=lambda:repo
        try:
            for prefix in ("/api","/api/v1"):
                status,body=asyncio.run(api_get(prefix+"/stocks/00631L/profile"))
                self.assertEqual((status,body["security_type"]),(200,"ETF"))
            self.assertEqual(asyncio.run(api_get("/api/stocks/00631l/profile"))[0],422)
        finally:app.dependency_overrides.pop(repository,None)
