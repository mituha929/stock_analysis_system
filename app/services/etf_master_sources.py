"""Parsers for the exchanges' public ETF listings, not fund establishment dates."""
from html import unescape
import json
import re

from app.services.stock_master_sources import (
    StockRecord, StockSnapshot, StockSourceError, _unique_object, parse_official_date,
)

TWSE_ETF_FIELDS = ["上市日期", "證券代號", "證券簡稱", "發行人", "標的指數"]
TPEX_ETF_FIELDS = frozenset(("issuerID", "listingDate", "issuerLink", "stockName", "totalAv", "holders",
                            "indexName", "valueYTD", "volumeYTD", "rorYTD", "stockNo", "issuer"))
TPEX_ETF_TOP = frozenset(("factorList", "rangeRorYTD", "rangeRor1M", "data", "rangeRor3M", "issuerList", "rangeTotalAv", "status"))


def _parts(value, *, optional=False):
    if optional and (value is None or isinstance(value, str) and not value.strip()):
        return [""]
    if not isinstance(value, str):
        raise StockSourceError("ETF 日期／代號／名稱必須是字串")
    pieces = [unescape(part).strip() for part in re.split(r"<br\s*/?>", value, flags=re.I)]
    if any(not piece or "<" in piece or ">" in piece for piece in pieces):
        raise StockSourceError("ETF 欄位含未知標記或空的加掛欄位")
    return pieces


def _currency_map(parts, kind):
    result = {}
    for part in parts:
        match = re.fullmatch(r"(.+)\(([^()]+)\)", part)
        if not match or match[2] in result:
            raise StockSourceError(f"ETF 加掛{kind}缺少唯一幣別標籤")
        result[match[2]] = match[1] if kind != "名稱" else part
    return result


def _twse_records(rows):
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            raise StockSourceError("TWSE ETF 欄位數不符")
        dates, codes, names = _parts(row[0], optional=True), _parts(row[1]), _parts(row[2])
        if len(dates) != len(codes) or len(codes) != len(names):
            raise StockSourceError("ETF 外幣加掛日期／代號／名稱筆數不一致")
        if len(codes) == 1:
            yield StockRecord(codes[0], names[0], "TWSE", parse_official_date(dates[0], optional=True), None, None, None, "ETF")
        else:
            # Match by currency, never copy the TWD date to the foreign-currency code.
            date_map, code_map, name_map = (_currency_map(parts, kind) for parts, kind in (
                (dates, "日期"), (codes, "代號"), (names, "名稱")))
            if set(date_map) != set(code_map) or set(code_map) != set(name_map):
                raise StockSourceError("ETF 外幣加掛幣別不一致")
            for currency, code in code_map.items():
                yield StockRecord(code, name_map[currency], "TWSE", parse_official_date(date_map[currency], optional=True), None, None, None, "ETF")


def parse_etf_snapshot(payload, market, today, *, minimum=50):
    try:
        data = json.loads(payload, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise StockSourceError("ETF 來源不是有效 JSON") from error
    if not isinstance(data, dict):
        raise StockSourceError("ETF 來源結構無效")
    if market == "TWSE":
        if set(data) != {"stat", "fields", "data"} or data["stat"] != "OK" or data["fields"] != TWSE_ETF_FIELDS:
            raise StockSourceError("TWSE ETF 回應狀態或欄位已變更")
    elif market == "TPEx":
        if set(data) != TPEX_ETF_TOP or data["status"] != "success":
            raise StockSourceError("TPEx ETF 回應狀態或欄位已變更")
    else:
        raise StockSourceError("未知 ETF 市場")
    rows = data["data"]
    if not isinstance(rows, list) or not minimum <= len(rows) <= 20000:
        raise StockSourceError("ETF 名單為空或筆數異常")
    if market == "TWSE":
        records = list(_twse_records(rows))
    else:
        records = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != TPEX_ETF_FIELDS:
                raise StockSourceError("TPEx ETF 資料列欄位已變更")
            if not isinstance(row["stockName"], str):
                raise StockSourceError("ETF 名稱無效")
            records.append(StockRecord(row["stockNo"], row["stockName"].strip(), market,
                                       parse_official_date(row["listingDate"], optional=True), None, None, None, "ETF"))
    codes, warnings = set(), []
    for record in records:
        record.validate()
        if record.code in codes:
            raise StockSourceError("ETF 清單有重複代號")
        codes.add(record.code)
        if record.listing_date is None:
            warnings.append(f"{record.code}: 官方 ETF 掛牌日期缺少，不推測")
    if not any(r.listing_date and r.listing_date <= today for r in records):
        raise StockSourceError("ETF 整批無有效掛牌日期")
    # These APIs publish no as-of date. Preserve that fact, not an invented report date.
    return StockSnapshot(market, today, tuple(records), tuple(warnings), "etf", "retrieval_date")
