"""One-shot public website quote snapshots; deliberately independent of Capital DLL."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CNYES_PAGE = "https://invest.cnyes.com/indices/major"
CMONEY_PAGE = "https://www.cmoney.tw/forum/futures/TXF1?s=p"
INDEX_NAMES = {"DJI": "道瓊指數", "INX": "S&P 500", "IXIC": "NASDAQ", "SOX": "費城半導體"}
EXPECTED_KEYS = {("cnyes", symbol, "index") for symbol in INDEX_NAMES} | {("cmoney", "TXF1", "night")}
CM_FIELDS = {
    "last_price": "成交", "change_value": "漲跌", "change_percent": "漲跌幅",
    "open_price": "開盤", "high_price": "最高", "low_price": "最低",
    "amount_yi": "金額(億)", "total_volume": "總量",
}


class QuoteFetchError(ValueError):
    """Missing/invalid source data: never substitute zeros or example values."""


def number(value: object, field: str, *, percent: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise QuoteFetchError(f"{field}: 缺值或非數字")
    text = str(value).strip().replace("−", "-")
    if percent and text.endswith("%"):
        text = text[:-1]
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        raise QuoteFetchError(f"{field}: 數值格式不正確")
    try:
        result = Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise QuoteFetchError(f"{field}: 數值格式不正確") from error
    if not result.is_finite() or abs(result) >= Decimal("1e16") or result != result.quantize(Decimal("0.00000001")):
        raise QuoteFetchError(f"{field}: 超出 DECIMAL(24,8) 範圍／精度")
    return result


def volume_number(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise QuoteFetchError("總量缺值或非整數")
    text = str(value).strip()
    if not re.fullmatch(r"\+?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.0+)?", text):
        raise QuoteFetchError("總量必須是非負整數")
    result = Decimal(text.replace(",", ""))
    if result > 18446744073709551615:
        raise QuoteFetchError("總量超過 unsigned BIGINT 上限")
    return int(result)


@dataclass(frozen=True)
class MarketQuote:
    source: str
    symbol: str
    name: str
    session: str
    source_url: str
    fetched_at: datetime
    quote_at: datetime | None
    last_price: Decimal
    change_value: Decimal
    change_percent: Decimal  # Percentage points: 1.00 means +1.00%, NOT 0.01.
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    amount_yi: Decimal | None = None
    total_volume: int | None = None
    raw: str = "{}"

    def validate(self) -> None:
        if (self.source, self.symbol, self.session) not in EXPECTED_KEYS:
            raise QuoteFetchError("不在允許的五個來源／商品／盤別中")
        for key in ("last_price", "change_value", "change_percent", "open_price", "high_price", "low_price"):
            value = getattr(self, key)
            if not isinstance(value, Decimal) or number(value, key) != value:
                raise QuoteFetchError(f"{key}: 必須使用精確 Decimal")
        if self.low_price <= 0 or not self.low_price <= self.open_price <= self.high_price or not self.low_price <= self.last_price <= self.high_price:
            raise QuoteFetchError(f"{self.symbol}: OHLC 價格範圍不一致")
        for key in ("fetched_at", "quote_at"):
            value = getattr(self, key)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise QuoteFetchError(f"{key}: 必須明確包含時區")
        if self.quote_at and self.quote_at > self.fetched_at + timedelta(minutes=5):
            raise QuoteFetchError(f"{self.symbol}: 來源時間在未來")
        if self.source == "cmoney":
            if self.source_url != CMONEY_PAGE or self.quote_at is not None:
                raise QuoteFetchError("CMoney 必須是夜盤網址；頁面未提供可確認的成交時間")
            if not isinstance(self.amount_yi, Decimal) or number(self.amount_yi, "金額(億)") < 0:
                raise QuoteFetchError("金額(億) 無效")
            if type(self.total_volume) is not int or not 0 <= self.total_volume <= 18446744073709551615:
                raise QuoteFetchError("總量必須是 unsigned BIGINT 整數")
        elif self.quote_at is None or self.source_url != CNYES_PAGE or self.amount_yi is not None or self.total_volume is not None:
            raise QuoteFetchError("美股指數來源時間／欄位無效")
        try:
            if not isinstance(json.loads(self.raw), dict):
                raise ValueError()
        except (ValueError, TypeError) as error:
            raise QuoteFetchError("raw 必須是 JSON object") from error

    def to_dict(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, datetime) else value
                for key, value in asdict(self).items() if key != "raw"} | {
            "quote_time_status": "source_timestamp" if self.quote_at else "not_provided",
        }


def validate_batch(quotes: list[MarketQuote]) -> None:
    if len(quotes) != 5 or {(q.source, q.symbol, q.session) for q in quotes} != EXPECTED_KEYS:
        raise QuoteFetchError("必須完整取得四個美股指數及一筆 TXF1 夜盤；不允許部分寫入")
    for quote in quotes:
        quote.validate()


def read_public_text(url: str, timeout: float = 25) -> str:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; CapitalMarketSnapshot/1.0)",
        "Accept": "application/json,text/html;q=0.9", "Cache-Control": "no-cache",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise QuoteFetchError("來源重新導向，停止抓取以避免誤取其他盤別／登入頁")
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise QuoteFetchError("來源回應過大")
            return data.decode("utf-8-sig")
    except (HTTPError, URLError, TimeoutError, UnicodeError, OSError) as error:
        # Do not include environment credentials/proxy details in errors.
        status = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
        raise QuoteFetchError(f"公開來源讀取失敗：{status}；未繞過封鎖／未改用範例資料") from error


def parse_cnyes(payload: str, symbol: str, fetched_at: datetime) -> MarketQuote:
    try:
        data = json.loads(payload, parse_float=Decimal)
        rows = data["data"]
        if data["statusCode"] != 200 or not isinstance(rows, list) or len(rows) != 1:
            raise QuoteFetchError("鉅亨回應狀態／筆數不符")
        row = rows[0]
        if symbol not in INDEX_NAMES or row["0"] != f"GI:{symbol}:INDEX" or row["200009"] != INDEX_NAMES[symbol]:
            raise QuoteFetchError("鉅亨回傳商品不符")
        epoch = row["200007"]
        if type(epoch) is not int or epoch <= 0:
            raise QuoteFetchError("鉅亨缺少有效報價時間")
        fields = {key: number(row[code], key) for key, code in {
            "last_price": "6", "change_value": "11", "change_percent": "56",
            "open_price": "19", "high_price": "12", "low_price": "13",
        }.items()}
        quote = MarketQuote("cnyes", symbol, INDEX_NAMES[symbol], "index", CNYES_PAGE, fetched_at,
                            datetime.fromtimestamp(epoch, timezone.utc), **fields,
                            raw=json.dumps(row, ensure_ascii=False, default=str))
        quote.validate()
        return quote
    except (KeyError, TypeError, OverflowError, OSError, json.JSONDecodeError) as error:
        raise QuoteFetchError(f"鉅亨 {symbol} 資料結構或時間無效") from error


class _QuoteHTML(HTMLParser):
    """Read JSON-LD and summary row spans, never execute the website's JavaScript."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.pairs: dict[str, list[str]] = {}
        self._script: list[str] | None = None
        self._span: list[str] | None = None
        self._kind = ""
        self._key = ""

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "script" and attr.get("type") == "application/ld+json":
            self._script = []
        if tag == "span":
            classes = (attr.get("class") or "").split()
            kind = "key" if "row__key" in classes else "value" if "row__value" in classes else ""
            if kind:
                self._kind, self._span = kind, []

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self._span is not None:
            self._span.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None
        if tag == "span" and self._span is not None:
            text = "".join(self._span).strip()
            if self._kind == "key":
                self._key = text
            else:
                self.pairs.setdefault(self._key, []).append(text)
                self._key = ""
            self._span = None


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def parse_cmoney(html: str, fetched_at: datetime) -> MarketQuote:
    try:
        parser = _QuoteHTML()
        parser.feed(html)
        nodes = [node for script in parser.scripts for node in _nodes(json.loads(script, parse_float=Decimal))]
        pages = [n for n in nodes if n.get("@type") == "WebPage" and n.get("url") == CMONEY_PAGE and "夜盤" in n.get("name", "")]
        entities = [n for n in nodes if n.get("@id") == CMONEY_PAGE + "#entity" and n.get("tickerSymbol") == "TXF1"]
        if len(pages) != 1 or len(entities) != 1:
            raise QuoteFetchError("CMoney 無法確認 TXF1 夜盤頁面／報價主體")
        properties: dict[str, list] = {}
        for item in entities[0]["additionalProperty"]:
            properties.setdefault(item["name"], []).append(item["value"])
        values = {}
        for field, label in CM_FIELDS.items():
            structured, visible = properties.get(label, []), parser.pairs.get(label, [])
            if len(structured) != 1 or len(visible) != 1:
                raise QuoteFetchError(f"CMoney {label} 缺少／重複，拒絕混用圖表提示值")
            parse_value = volume_number if field == "total_volume" else lambda v: number(v, label, percent=field == "change_percent")
            value = parse_value(structured[0])
            if value != parse_value(visible[0]):
                raise QuoteFetchError(f"CMoney {label} 結構化資料與報價面板不一致")
            values[field] = value
        quote = MarketQuote("cmoney", "TXF1", "台指期夜盤", "night", CMONEY_PAGE, fetched_at, None,
                            **values, raw=json.dumps({"properties": properties,
                                "page_date_modified": pages[0].get("dateModified"),
                                "quote_time_note": "頁面日期不是成交時間；未推算交易日",
                                "amount_unit": "億", "volume_source_label": "總量"}, ensure_ascii=False, default=str))
        quote.validate()
        return quote
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise QuoteFetchError("CMoney 公開頁面資料結構無效") from error


def fetch_all(*, reader=read_public_text) -> list[MarketQuote]:
    quotes = []
    for symbol in INDEX_NAMES:
        url = f"https://ws.api.cnyes.com/ws/api/v1/quote/quotes/GI:{symbol}:INDEX?column=G,QUOTES"
        payload = reader(url)
        quotes.append(parse_cnyes(payload, symbol, datetime.now(timezone.utc)))
    html = reader(CMONEY_PAGE)
    quotes.append(parse_cmoney(html, datetime.now(timezone.utc)))
    validate_batch(quotes)
    return quotes


def fetch_one(source: str, symbol: str, session: str, *, reader=read_public_text) -> MarketQuote:
    if (source, symbol, session) not in EXPECTED_KEYS:
        raise QuoteFetchError("不在允許的五個來源／商品／盤別中")
    if source == "cnyes":
        payload = reader(f"https://ws.api.cnyes.com/ws/api/v1/quote/quotes/GI:{symbol}:INDEX?column=G,QUOTES")
        return parse_cnyes(payload, symbol, datetime.now(timezone.utc))
    return parse_cmoney(reader(CMONEY_PAGE), datetime.now(timezone.utc))
