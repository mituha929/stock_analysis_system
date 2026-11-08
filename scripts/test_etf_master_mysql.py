"""Real legacy-schema -> ETF migration acceptance, only in an owned UUID schema."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import asyncio
import json
import re
import sys
import traceback
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))

from app.services.mysql_instrument_repository import MySQLSettings
from app.services.stock_master_sources import SOURCES,TAIPEI,StockRecord,StockSnapshot,StockSourceError
from app.services.stock_master_repository import DATA_COLUMNS,StockMasterRepository,StockSyncError,encoded


def checks(repo):
    results=[]
    now=datetime.now(timezone.utc)
    today=now.astimezone(TAIPEI).date()
    stamp=(now-timedelta(days=1)).replace(tzinfo=None)
    baseline=[StockRecord("2330","測試台積電","TWSE",date(1994,9,5),"半導體業","24","22099131"),
              StockRecord("6488","測試環球晶","TPEx",date(2015,9,25),"半導體業","24","28113286"),
              StockRecord("910322","測試存託憑證","TWSE",date(2009,12,16),None,"91","12345678","DR")]
    legacy_columns=tuple(c for c in DATA_COLUMNS if c not in {"security_type","source_date_kind"})
    old_rows=[]
    con=repo._connect_database(repo._connector());cur=con.cursor()
    try:
        cur.execute("""CREATE TABLE stock_master (
          code VARCHAR(6) NOT NULL,name VARCHAR(128) NOT NULL,market VARCHAR(4) NOT NULL,
          listing_date DATE NULL,industry VARCHAR(64) NULL,updated_at DATETIME(6) NOT NULL,
          industry_code VARCHAR(2) NULL,issuer_id VARCHAR(8) NULL,source_date DATE NOT NULL,source_url VARCHAR(256) NOT NULL,
          first_seen_at DATETIME(6) NOT NULL,last_seen_at DATETIME(6) NOT NULL,presence_status VARCHAR(24) NOT NULL,
          date_status VARCHAR(32) NOT NULL,reported_listing_date DATE NULL,last_run_id CHAR(32) NOT NULL,
          PRIMARY KEY(code,market),INDEX idx_stock_presence(market,presence_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        cur.execute("CREATE TABLE stock_listing_plans LIKE stock_master")
        cur.execute("""CREATE TABLE stock_master_history (
          id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,run_id CHAR(32) NOT NULL,code VARCHAR(6) NOT NULL,
          market VARCHAR(4) NOT NULL,object_type VARCHAR(12) NOT NULL,changed_at DATETIME(6) NOT NULL,
          before_json LONGTEXT NULL,after_json LONGTEXT NOT NULL,
          INDEX idx_stock_history(code,market,id),INDEX idx_stock_history_run(run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        for record in baseline:
            row=dict(zip(legacy_columns,(record.code,record.name,record.market,record.listing_date,record.industry,stamp,
                record.industry_code,record.issuer_id,today,SOURCES[record.market],stamp,stamp,"present","provided",record.listing_date,"0"*32)))
            old_rows.append(row)
            cur.execute("INSERT INTO stock_master ("+",".join("`"+c+"`" for c in legacy_columns)+") VALUES ("+",".join(["%s"]*len(legacy_columns))+")",tuple(row[c] for c in legacy_columns))
            cur.execute("INSERT INTO stock_master_history(run_id,code,market,object_type,changed_at,before_json,after_json) VALUES(%s,%s,%s,'master',%s,NULL,%s)",("0"*32,record.code,record.market,stamp,encoded(row)))
        con.commit()
    finally:cur.close();con.close()

    company=[StockSnapshot(market,today,tuple(r for r in baseline if r.market==market)) for market in SOURCES]
    # Materialized tuples, explicit trading-code dates; no fund-establishment fallback.
    etfs=[StockSnapshot("TWSE",today,tuple(StockRecord(code,name,"TWSE",listed,None,None,None,"ETF") for code,name,listed in (
        ("0050","元大台灣50",date(2003,6,30)),("00631L","元大台灣50正2",date(2014,10,31)),
        ("006205","富邦上証(新臺幣)",date(2011,9,26)),("00625K","富邦上証+R(人民幣)",date(2016,8,8)),
        ("009831","測試未來ETF",today+timedelta(days=6)))),(),"etf","retrieval_date"),
        StockSnapshot("TPEx",today,(StockRecord("00679B","元大美債20年","TPEx",date(2017,1,17),None,None,None,"ETF"),
            StockRecord("00411A","主動統一前沿科技","TPEx",date(2026,8,26),None,None,None,"ETF"),
            StockRecord("00980D","測試主動債券ETF","TPEx",date(2026,1,22),None,None,None,"ETF")),(),"etf","retrieval_date")]
    def fetch():return company+etfs
    def sync(fetcher=fetch):return repo.synchronize(fetcher=fetcher,trigger="test")
    def query(sql,params=()):
        connection=repo._connect_database(repo._connector());connection.start_transaction(readonly=True);cursor=connection.cursor(dictionary=True)
        try:cursor.execute(sql,params);return cursor.fetchall()
        finally:connection.rollback();cursor.close();connection.close()

    initial=sync()
    assert initial["new"]==7 and initial["changed"]==1 and initial["unchanged"]==2 and initial["scheduled"]==1
    results.append("legacy_schema_additive_migration_with_existing_data")
    for row in old_rows:
        profile=repo.profile(row["code"])
        assert all(profile[k]==(row[k].isoformat() if k=="listing_date" else row[k]) for k in ("code","name","market","listing_date","industry"))
    assert repo.profile("2330")["updated_at"]==stamp.replace(tzinfo=timezone.utc).astimezone(TAIPEI).isoformat()
    results.extend(["all_legacy_company_dates_names_and_industries_preserved","unchanged_stock_timestamp_preserved"])
    old_history=query("SELECT after_json FROM stock_master_history WHERE run_id=%s ORDER BY id",("0"*32,))
    assert [r["after_json"] for r in old_history]==[encoded(r) for r in old_rows]
    results.append("legacy_history_json_untouched")
    assert repo.profile("910322")["security_type"]=="DR"
    results.append("existing_tdr_classification_explicit_not_etf")
    assert repo.profile("0050")["security_type"]=="ETF" and repo.profile("0050")["listing_date"]=="2003-06-30"
    assert repo.profile("00631L")["listing_date"]=="2014-10-31"
    assert repo.profile("006205")["listing_date"]=="2011-09-26" and repo.profile("00625K")["listing_date"]=="2016-08-08"
    results.extend(["etf_in_same_mysql_master_with_type","alphanumeric_etf_code_supported","foreign_currency_listing_dates_distinct"])
    assert repo.profile("00679B")["market"]=="TPEx" and repo.profile("00411A")["security_type"]=="ETF"
    assert repo.profile("0050")["source"]["source_date_kind"]=="retrieval_date"
    assert repo.profile("2330")["source"]["source_date_kind"]=="official_report_date"
    results.extend(["otc_bond_and_active_etfs","retrieval_date_not_misrepresented_as_report_date"])
    try:repo.profile("009831")
    except KeyError:pass
    else:raise AssertionError("Future ETF incorrectly active")
    assert repo.listing_plans("009831")["items"][0]["security_type"]=="ETF"
    results.append("future_etf_only_in_listing_plans")
    history_before=query("SELECT COUNT(*) AS n FROM stock_master_history")[0]["n"]
    repeat=sync()
    assert repeat["new"]==0 and repeat["changed"]==0 and repeat["unchanged"]==10
    assert query("SELECT COUNT(*) AS n FROM stock_master_history")[0]["n"]==history_before
    results.append("repeat_no_duplicate_etfs_or_history")
    sync(fetcher=lambda:company)
    assert repo.profile("00625K")["source"]["presence_status"]=="present"
    assert repo.listing_plans("009831")["items"][0]["presence_status"]=="present"
    assert "TWSE:ETF" in repo.sync_runs()["items"][0]["summary"]["last_known_sources"]
    results.append("company_only_sync_keeps_etf_membership_and_baseline")
    invalid=lambda: company+[replace(etfs[0],records=(replace(etfs[0].records[0],name="不應提交"),))]
    try:sync(fetcher=invalid)
    except StockSyncError:pass
    else:raise AssertionError("Partial ETF pair accepted")
    assert repo.profile("0050")["name"]=="元大台灣50"
    results.append("partial_etf_source_failure_cannot_overwrite")
    import app.services.stock_master_repository as module
    real_write=module._write_row
    def break_etf(cursor,table,row):
        real_write(cursor,table,row)
        if row["security_type"]=="ETF":raise RuntimeError("intentional after ETF SQL write")
    with patch.object(module,"_write_row",side_effect=break_etf):
        try:sync()
        except StockSyncError:pass
        else:raise AssertionError("ETF transaction did not roll back")
    assert query("SELECT COUNT(*) AS n FROM stock_master_history")[0]["n"]==history_before
    results.append("real_etf_sql_failure_rolls_back_full_batch")
    retained=replace(etfs[0],records=(replace(etfs[0].records[0],listing_date=None),*etfs[0].records[1:]))
    sync(fetcher=lambda:company+[retained,etfs[1]])
    assert repo.profile("0050")["listing_date"]=="2003-06-30"
    assert repo.profile("0050")["source"]["date_status"]=="retained_previous_official"
    results.append("missing_etf_date_preserves_previous_official_date")
    activated=replace(etfs[0],records=tuple(replace(r,listing_date=today) if r.code=="009831" else r for r in etfs[0].records))
    sync(fetcher=lambda:company+[activated,etfs[1]])
    assert repo.profile("009831")["security_type"]=="ETF" and repo.listing_plans("009831")["items"][0]["presence_status"]=="activated"
    results.append("future_plan_activated_only_after_source_confirms_date")

    from app.main import app
    from app.api.stock_profiles import repository
    async def get(path):
        output=[]
        async def receive():return {"type":"http.request","body":b"","more_body":False}
        async def send(msg):output.append(msg)
        await app({"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":"GET","scheme":"http",
            "path":path,"raw_path":path.encode(),"query_string":b"","headers":[],"client":("127.0.0.1",1),"server":("test",80),"root_path":""},receive,send)
        return output[0]["status"],json.loads(b"".join(m.get("body",b"") for m in output[1:]))
    app.dependency_overrides[repository]=lambda:repo
    try:
        status,body=asyncio.run(get("/api/stocks/00631L/profile"))
        assert status==200 and body["security_type"]=="ETF" and body["listing_date"]=="2014-10-31"
    finally:app.dependency_overrides.pop(repository,None)
    results.append("real_mysql_etf_fastapi_profile")
    return results


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT/".env",override=False)
    settings=MySQLSettings.from_env();schema="etf_master_test_"+uuid.uuid4().hex
    if not re.fullmatch(r"etf_master_test_[0-9a-f]{32}",schema) or schema==settings.database:raise RuntimeError("Unsafe schema")
    import mysql.connector
    con=mysql.connector.connect(host=settings.host,port=settings.port,user=settings.user,password=settings.password,connection_timeout=10)
    cursor=con.cursor();created=False
    try:
        cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");created=True
        result=checks(StockMasterRepository(replace(settings,database=schema)))
    finally:
        if created:
            if not re.fullmatch(r"etf_master_test_[0-9a-f]{32}",schema) or schema==settings.database:raise RuntimeError("Unsafe cleanup")
            cursor.execute(f"DROP DATABASE `{schema}`")
        cursor.close();con.close()
    print(json.dumps({"status":"passed","checks":result,"test_schema_removed":created,"production_rows_written":0},ensure_ascii=False))


if __name__=="__main__":
    try:main()
    except Exception as error:
        frame=traceback.extract_tb(error.__traceback__)[-1]
        print(json.dumps({"status":"failed","error_type":type(error).__name__,"line":frame.lineno,
            "reason":str(error) if isinstance(error,StockSyncError) else None}),file=sys.stderr)
        raise SystemExit(1)
