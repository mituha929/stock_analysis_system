"""Offline tests plus an explicitly requested, one-shot official endpoint check.

python tests/test_market_data_smoke.py                 # offline only
python tests/test_market_data_smoke.py --live          # 12 data URLs, once each
python tests/test_market_data_smoke.py --replay PATH   # reanalyse saved bytes

Endpoint availability is only the first MVP gate. It does NOT certify units,
publication dates, accounting basis, or the complete six-dataset acceptance.
No app.main, dotenv, Capital, database, or scheduler imports are used here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


TAIPEI = timezone(timedelta(hours=8))
OUTPUT = Path(__file__).resolve().parent / "output" / "endpoint_validation"
MAX_BYTES = 20_000_000
USER_AGENT = "CapitalMarketDataSmoke/1.0 (official public data verification)"


@dataclass(frozen=True)
class Endpoint:
    test_id: str
    market: str
    category: str
    url: str

    @property
    def key(self):
        return f"{self.test_id}_{self.market}"


ENDPOINTS = (
    Endpoint("M01", "TWSE", "個股三大法人", "https://www.twse.com.tw/rwd/zh/fund/T86?response=json"),
    Endpoint("M01", "TPEx", "個股三大法人", "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"),
    Endpoint("M02", "TWSE", "市場三大法人", "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"),
    Endpoint("M02", "TPEx", "市場三大法人", "https://www.tpex.org.tw/openapi/v1/tpex_3insti_summary"),
    Endpoint("M03", "TWSE", "融資融券", "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"),
    Endpoint("M03", "TPEx", "融資融券", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"),
    Endpoint("M04", "TWSE", "月營收", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
    Endpoint("M04", "TPEx", "月營收", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"),
    Endpoint("M05", "TWSE", "季度損益表（一般業）", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"),
    Endpoint("M05", "TPEx", "季度損益表（一般業）", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci"),
    Endpoint("M06", "TWSE", "季度資產負債表（一般業）", "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci"),
    Endpoint("M06", "TPEx", "季度資產負債表（一般業）", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci"),
)

# Minimal field contracts from the saved official responses. This is NOT a
# normalized financial schema; units/accounting basis still require verification.
REQUIRED_FIELDS = {
    "M01_TWSE": {"證券代號", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數"},
    "M01_TPEx": {"Date", "SecuritiesCompanyCode", "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
                 "SecuritiesInvestmentTrustCompanies-Difference", "Dealers-Difference"},
    "M02_TWSE": {"單位名稱", "買進金額", "賣出金額", "買賣差額"},
    "M02_TPEx": {"Date", "Investor", "PurchaseAmount", "SaleAmount", "Net"},
    "M03_TWSE": {"股票代號", "融資今日餘額", "融券今日餘額"},
    "M03_TPEx": {"Date", "SecuritiesCompanyCode", "MarginPurchaseBalance", "ShortSaleBalance"},
    "M04_TWSE": {"出表日期", "資料年月", "公司代號", "營業收入-當月營收"},
    "M04_TPEx": {"出表日期", "資料年月", "公司代號", "營業收入-當月營收"},
    "M05_TWSE": {"出表日期", "年度", "季別", "公司代號", "營業收入"},
    "M05_TPEx": {"Date", "Year", "Season", "SecuritiesCompanyCode", "營業收入"},
    "M06_TWSE": {"出表日期", "年度", "季別", "公司代號", "資產總計", "負債總計"},
    "M06_TPEx": {"Date", "年度", "季別", "SecuritiesCompanyCode", "資產總計", "負債總計"},
}
CODE_FIELDS = ("證券代號", "股票代號", "公司代號", "SecuritiesCompanyCode")


def raw_period(sample, envelope):
    """Keep source dates distinct; never infer a trading date from HTTP Date."""
    observations = {key: sample[key] for key in (
        "Date", "出表日期", "資料年月", "年度", "季別", "Year", "Season",
    ) if key in sample}
    if "date" in envelope:
        observations["response.date"] = envelope["date"]
    return observations


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_once(endpoint: Endpoint, *, opener=None):
    """One GET; no retries, redirect follow, credentials, or alternate sources."""
    started = datetime.now(TAIPEI)
    result = {
        "key": endpoint.key, "test_id": endpoint.test_id, "market": endpoint.market,
        "category": endpoint.category, "source_url": endpoint.url,
        "fetched_at": started.isoformat(), "http_status": None,
        "content_type": None, "error": None, "truncated": False,
    }
    request = Request(endpoint.url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json,text/csv,text/html;q=0.5",
    })
    response = None
    try:
        try:
            response = (opener or build_opener(NoRedirect())).open(request, timeout=15)
        except HTTPError as error:
            response = error  # Save the actual error body and status as evidence.
        with response:
            result["http_status"] = response.code
            result["content_type"] = response.headers.get("Content-Type", "")
            result["response_url"] = response.geturl()
            result["response_headers"] = {key: response.headers.get(key) for key in (
                "Date", "Last-Modified", "Retry-After", "Content-Length",
            ) if response.headers.get(key) is not None}
            raw = response.read(MAX_BYTES + 1)
            result["truncated"] = len(raw) > MAX_BYTES
            if result["truncated"]:
                raw = raw[:MAX_BYTES]
        if result["http_status"] != 200:
            result["error"] = f"HTTP {result['http_status']}"
    except (URLError, TimeoutError, OSError) as error:
        raw = b""
        result["error"] = type(error).__name__
        # Do not log proxy URLs, credentials or machine environment.
        if isinstance(error, URLError):
            result["network_reason_type"] = type(error.reason).__name__
    result["elapsed_seconds"] = round((datetime.now(TAIPEI) - started).total_seconds(), 3)
    result["bytes_saved"] = len(raw)
    return result, raw


def json_rows(payload):
    """Accept known JSON layouts, never treat an error/message object as a row."""
    if isinstance(payload, list):
        if payload and all(isinstance(row, dict) and row for row in payload):
            return payload
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("stat") not in (None, "OK"):
        return []
    fields, rows = payload.get("fields"), payload.get("data")
    if isinstance(fields, list) and fields and isinstance(rows, list):
        if all(isinstance(name, str) for name in fields) and len(set(fields)) == len(fields):
            return [dict(zip(fields, row)) for row in rows
                    if isinstance(row, list) and len(row) == len(fields)]
    tables = payload.get("tables")
    if isinstance(tables, list):
        return [row for table in tables for row in json_rows(table)]
    return []


def inspect_response(result, raw):
    result = dict(result)
    result.update(endpoint_status="pending", mvp_status="pending", row_count=0,
                  detected_format=None, fields=[], sample=None)
    if result.get("http_status") != 200 or result.get("error"):
        result["endpoint_status"] = "failed"
        return result
    if result.get("truncated"):
        result.update(endpoint_status="failed", error="response_size_limit")
        return result
    if not raw.strip():
        result["error"] = "empty_body"
        return result
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        result["error"] = "unverified_encoding"
        return result
    if text.lstrip().startswith(("<", "<!")):
        result.update(detected_format="HTML", error="html_requires_source_verification")
        return result
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # CSV is recorded rather than parsed using a guessed delimiter/schema.
        result["detected_format"] = "CSV" if "csv" in result.get("content_type", "").lower() else "text"
        result["error"] = "unverified_response_format"
        return result
    result["detected_format"] = "JSON"
    if isinstance(payload, dict):
        result["envelope"] = {key: payload[key] for key in ("stat", "date", "title", "notes") if key in payload}
    rows = json_rows(payload)
    if not rows:
        result["error"] = "empty_or_unrecognized_data"
        return result
    required = REQUIRED_FIELDS.get(result.get("key"), set())
    candidates = [row for row in rows if required <= row.keys()]
    if not candidates:
        result.update(error="required_fields_missing", expected_fields=sorted(required))
        return result
    # Stock MVP examples must be stocks, not the ETF often at the head of a list.
    if result.get("test_id") != "M02" and required:
        candidates = [row for row in candidates if any(
            isinstance(row.get(key), str) and len(row[key]) == 4 and row[key].isascii() and row[key].isdigit()
            for key in CODE_FIELDS)]
        if not candidates:
            result["error"] = "no_four_digit_stock_sample"
            return result
    preferred = "2330" if result.get("market") == "TWSE" else "6488"
    sample = next((row for row in candidates if any(row.get(key) == preferred for key in CODE_FIELDS)), candidates[0])
    result.update(endpoint_status="data_available", row_count=len(rows),
                  fields=list(sample), sample=sample)
    result["observed_period_fields"] = raw_period(sample, result.get("envelope", {}))
    result["sample_stock_code"] = next((sample[k] for k in CODE_FIELDS if k in sample), None)
    result["data_issues"] = []
    if not result["observed_period_fields"]:
        result["data_issues"].append("回應沒有資料日期；不得以 HTTP Date 或抓取時間代替交易日。")
    result["mvp_note"] = "僅確認端點回傳非空資料；日期、單位、欄位語意及各類資料驗收仍待核對。"
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_report(folder, results):
    write_json(folder / "results.json", results)
    lines = ["# 官方資料端點最小連線驗證", "",
             "本報告只驗證端點。HTTP 200 不代表完整 MVP 通過；未查證單位、公告時間與財報口徑。",
             "每次驗證每個資料端點至多請求一次；無自動重試、重新導向、群益登入、資料庫操作或排程。",
             "data_available 表示已取得非空 JSON 且包含該來源必要欄位，不代表完整資料驗收通過。", "",
             "| ID | 市場 | 類別 | HTTP | Content-Type | 格式 | 筆數 | 端點結果 |",
             "|---|---|---|---|---|---|---:|---|"]
    for r in results:
        lines.append(f"| {r['test_id']} | {r['market']} | {r['category']} | {r.get('http_status') or '—'} | "
                     f"{r.get('content_type') or '—'} | {r.get('detected_format') or '—'} | {r.get('row_count', 0)} | "
                     f"{r['endpoint_status']} |")
    for r in results:
        lines += ["", f"## {r['key']}：{r['category']}", "",
                  f"- 來源：{r['source_url']}", f"- 抓取時間：{r['fetched_at']}",
                  f"- 端點驗證：{r['endpoint_status']}；完整 MVP：{r['mvp_status']}",
                  f"- 錯誤／待驗證原因：{r.get('error') or r.get('mvp_note', '無')}",
                  f"- 原始檔：raw/{r['key']}.bin；請求資訊：raw/{r['key']}.json",
                  f"- 樣本股票代號：{r.get('sample_stock_code') or '不適用／未取得'}",
                  f"- 日期／期間原欄位：{json.dumps(r.get('observed_period_fields', {}), ensure_ascii=False)}",
                  f"- 原始欄位：{'、'.join(r.get('fields', [])) or '未取得'}"]
        lines += [f"- 資料缺口：{issue}" for issue in r.get("data_issues", [])]
        if r.get("envelope"):
            lines += ["", "```json", json.dumps(r["envelope"], ensure_ascii=False, indent=2), "```"]
        if r.get("sample"):
            lines += ["", "原始樣本（未正規化）：", "", "```json",
                      json.dumps(r["sample"], ensure_ascii=False, indent=2), "```"]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_live(folder, *, fetcher=fetch_once):
    folder = Path(folder)
    # Refuse overwriting evidence or accidentally requesting the same run again.
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "raw").mkdir()
    results, blocked_hosts = [], set()
    for endpoint in ENDPOINTS:
        host = urlsplit(endpoint.url).hostname
        if host in blocked_hosts:
            meta = {"key": endpoint.key, "test_id": endpoint.test_id, "market": endpoint.market,
                    "category": endpoint.category, "source_url": endpoint.url,
                    "fetched_at": datetime.now(TAIPEI).isoformat(), "http_status": None,
                    "content_type": None, "error": "skipped_host_after_403_or_429"}
            raw = b""
        else:
            meta, raw = fetcher(endpoint)
            if meta.get("http_status") in (403, 429):
                blocked_hosts.add(host)
        (folder / "raw" / f"{endpoint.key}.bin").write_bytes(raw)
        write_json(folder / "raw" / f"{endpoint.key}.json", meta)
        result = inspect_response(meta, raw)
        if meta.get("error") == "skipped_host_after_403_or_429":
            result["endpoint_status"] = "skipped"
        results.append(result)
        # Persist completed evidence before continuing to the next endpoint.
        write_report(folder, results)
        print(f"{endpoint.key}: HTTP {meta.get('http_status')} / {result['endpoint_status']} / {result.get('row_count', 0)} rows", flush=True)
    return results


def replay(folder):
    folder = Path(folder)
    results = []
    for endpoint in ENDPOINTS:
        meta_path = folder / "raw" / f"{endpoint.key}.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result = inspect_response(meta, (folder / "raw" / f"{endpoint.key}.bin").read_bytes())
        if meta.get("error") == "skipped_host_after_403_or_429":
            result["endpoint_status"] = "skipped"
        results.append(result)
    write_report(folder, results)
    return results


class EndpointValidationTests(unittest.TestCase):
    def test_http_200_html_is_not_success(self):
        result = inspect_response({"http_status": 200, "content_type": "text/html"}, b"<html>blocked</html>")
        self.assertEqual(result["endpoint_status"], "pending")
        self.assertEqual(result["detected_format"], "HTML")

    def test_empty_and_error_json_never_pass(self):
        for raw in (b"[]", b"{}", b'{"error":"blocked"}', b'{"stat":"no data","fields":["Code"],"data":[["2330"]]}'):
            self.assertNotEqual(inspect_response({"http_status": 200}, raw)["endpoint_status"], "data_available")

    def test_http_error_keeps_status_and_is_not_success(self):
        result = inspect_response({"http_status": 429, "error": "HTTP 429"}, b"rate limited")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["endpoint_status"], "failed")

    def test_twse_table_and_tpex_array_preserve_original_fields(self):
        for payload in ({"stat": "OK", "fields": ["Code", "Value"], "data": [["2330", "-1,000"]]},
                        [{"Code": "2330", "Value": "-1,000"}]):
            result = inspect_response({"http_status": 200}, json.dumps(payload).encode())
            self.assertEqual(result["endpoint_status"], "data_available")
            self.assertEqual(result["sample"], {"Code": "2330", "Value": "-1,000"})
            self.assertEqual(result["mvp_status"], "pending")

    def test_host_block_stops_later_requests(self):
        from tempfile import TemporaryDirectory
        calls = []
        def blocked(endpoint):
            calls.append(endpoint.url)
            return {"key": endpoint.key, "test_id": endpoint.test_id, "market": endpoint.market,
                    "category": endpoint.category, "source_url": endpoint.url, "fetched_at": "test",
                    "http_status": 403, "error": "HTTP 403"}, b"blocked"
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            with patch("builtins.print"):
                results = run_live(Path(temporary) / "run", fetcher=blocked)
            self.assertEqual(len(calls), 3)  # three distinct hosts, no repeated blocked host
            self.assertEqual(sum(r["endpoint_status"] == "skipped" for r in results), 9)

    def test_no_redirect_follow_or_retry(self):
        handler = NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.com"))
        opener = Mock()
        opener.open.side_effect = URLError("test unavailable")
        result, raw = fetch_once(ENDPOINTS[0], opener=opener)
        opener.open.assert_called_once()
        self.assertEqual(raw, b"")
        self.assertEqual(result["error"], "URLError")

    def test_wrong_dataset_with_http_200_cannot_pass(self):
        meta = {"key": "M03_TWSE", "test_id": "M03", "market": "TWSE", "http_status": 200}
        result = inspect_response(meta, b'[{"message":"temporarily unavailable"}]')
        self.assertEqual(result["error"], "required_fields_missing")
        self.assertNotEqual(result["endpoint_status"], "data_available")

    def test_missing_date_is_recorded_instead_of_using_fetch_time(self):
        meta = {"key": "M03_TWSE", "test_id": "M03", "market": "TWSE", "http_status": 200,
                "fetched_at": "2026-11-05T13:00:00+08:00"}
        raw = json.dumps([{"股票代號": "2330", "融資今日餘額": "100", "融券今日餘額": "10"}]).encode()
        result = inspect_response(meta, raw)
        self.assertEqual(result["endpoint_status"], "data_available")
        self.assertEqual(result["observed_period_fields"], {})
        self.assertTrue(result["data_issues"])
        self.assertEqual(result["mvp_status"], "pending")

    def test_stock_sample_skips_etf(self):
        rows = [{"Date": "1151104", "SecuritiesCompanyCode": code,
                 "MarginPurchaseBalance": "100", "ShortSaleBalance": "10"}
                for code in ("00411A", "6488")]
        meta = {"key": "M03_TPEx", "test_id": "M03", "market": "TPEx", "http_status": 200}
        self.assertEqual(inspect_response(meta, json.dumps(rows).encode())["sample_stock_code"], "6488")

    def test_replay_never_calls_network(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            folder = Path(temporary)
            (folder / "raw").mkdir()
            endpoint = ENDPOINTS[0]
            write_json(folder / "raw" / f"{endpoint.key}.json", {
                "key": endpoint.key, "test_id": endpoint.test_id, "market": endpoint.market,
                "category": endpoint.category, "source_url": endpoint.url,
                "fetched_at": "offline", "http_status": 403, "error": "HTTP 403",
            })
            (folder / "raw" / f"{endpoint.key}.bin").write_bytes(b"blocked")
            with patch(__name__ + ".build_opener", side_effect=AssertionError("replay must stay offline")):
                results = replay(folder)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["endpoint_status"], "failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="明確啟動一次官方端點驗證")
    mode.add_argument("--replay", type=Path, help="僅用既存原始檔重建報告，不連線")
    parser.add_argument("--output", type=Path, help="--live 的新輸出目錄；不得已存在")
    args = parser.parse_args()
    if args.live:
        folder = args.output or OUTPUT / datetime.now(TAIPEI).strftime("%Y%m%dT%H%M%S%f%z")
        results = run_live(folder)
        print(f"Report: {folder / 'report.md'}")
        return 0 if all(r["endpoint_status"] == "data_available" for r in results) else 1
    if args.replay:
        replay(args.replay)
        print(f"Report: {args.replay / 'report.md'}")
        return 0
    if args.output:
        parser.error("--output 必須搭配 --live")
    unittest.main(argv=[sys.argv[0]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
