"""Official issuer rosters. No Capital login, quote data, or inferred listing dates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TAIPEI = timezone(timedelta(hours=8))
SOURCES = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "TPEx": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
}
ETF_SOURCES = {
    "TWSE": "https://www.twse.com.tw/rwd/zh/ETF/list?response=json",
    "TPEx": "https://info.tpex.org.tw/api/etfFilter",
}
CODE_PATTERN = r"(?:[0-9]{4,6}|[0-9]{5}[A-Z])"
# Deliberately pin the published schema, including unused columns. Schema drift
# stops the batch rather than accidentally substituting incorporation/ESB dates.
TWSE_FIELDS = frozenset((
    "出表日期", "公司代號", "公司名稱", "公司簡稱", "外國企業註冊地國", "產業別", "住址", "營利事業統一編號",
    "董事長", "總經理", "發言人", "發言人職稱", "代理發言人", "總機電話", "成立日期", "上市日期",
    "普通股每股面額", "實收資本額", "私募股數", "特別股", "編制財務報表類型", "股票過戶機構", "過戶電話",
    "過戶地址", "簽證會計師事務所", "簽證會計師1", "簽證會計師2", "英文簡稱", "英文通訊地址", "傳真機號碼",
    "電子郵件信箱", "網址", "已發行普通股數或TDR原股發行股數",
))
TPEX_FIELDS = frozenset((
    "Date", "SecuritiesCompanyCode", "CompanyName", "CompanyAbbreviation", "Registration", "SecuritiesIndustryCode",
    "Address", "UnifiedBusinessNo.", "Chairman", "GeneralManager", "Spokesman", "TitleOfSpokesman", "DeputySpokesperson",
    "Telephone", "DateOfIncorporation", "DateOfListing", "ParValueOfCommonStock", "Paidin.Capital.NTDollars",
    "PrivateStock.shares", "PreferredStock.shares", "PreparationOfFinancialReportType", "StockTransferAgent",
    "StockTransferAgentTelephone", "StockTransferAgentAddress", "AccountingFirm", "CPA.CharteredPublicAccountant.First",
    "CPA.CharteredPublicAccountant.Second", "Symbol", "Fax", "EmailAddress", "WebAddress", "IssueShares",
))
COMMON_INDUSTRIES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維", "05": "電機機械",
    "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業",
    "12": "汽車工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}
INDUSTRIES = {
    "TWSE": COMMON_INDUSTRIES | {"17": "金融保險", "91": None},
    "TPEx": COMMON_INDUSTRIES | {"17": "金融業", "22": "生技醫療", "32": "文化創意業", "33": "農業科技業", "80": "管理股票"},
}


class StockSourceError(ValueError):
    pass


def parse_official_date(value: object, *, optional=False) -> date | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "-", "--", "－", "N/A"}):
        if optional:
            return None
        raise StockSourceError("官方出表日期缺少")
    if not isinstance(value, str):
        raise StockSourceError("日期必須是字串")
    text = value.strip()
    match = re.fullmatch(r"(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        year_text, month_text, day_text = match.groups()
    elif re.fullmatch(r"\d{5,8}", text):
        year_text, month_text, day_text = text[:-4], text[-4:-2], text[-2:]
    else:
        raise StockSourceError("日期必須是西元或民國年月日，不得推測")
    year = int(year_text) + (1911 if len(year_text) <= 3 else 0)
    try:
        return date(year, int(month_text), int(day_text))
    except ValueError as error:
        raise StockSourceError("官方日期不存在") from error


def validate_code(code: object) -> str:
    if not isinstance(code, str) or not re.fullmatch(CODE_PATTERN, code):
        raise StockSourceError("代號必須是 4～6 位數字，或 5 位數字加一個大寫英文字母")
    return code


@dataclass(frozen=True)
class StockRecord:
    code: str
    name: str
    market: str
    listing_date: date | None
    industry: str | None
    industry_code: str | None
    issuer_id: str | None
    security_type: str = "STOCK"

    def validate(self):
        validate_code(self.code)
        if self.security_type not in {"STOCK", "DR", "ETF"}:
            raise StockSourceError("商品類型無效")
        if self.security_type != "ETF" and not re.fullmatch(r"[0-9]{4,6}", self.code):
            raise StockSourceError("公司主檔代號不得混入 ETF 英數代號")
        if self.market not in SOURCES or not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128:
            raise StockSourceError("市場或股票名稱無效")
        if self.listing_date is not None and type(self.listing_date) is not date:
            raise StockSourceError("掛牌日期格式無效")
        if self.industry is not None and (not isinstance(self.industry, str) or len(self.industry) > 64):
            raise StockSourceError("產業名稱無效")
        if self.industry_code is not None and not re.fullmatch(r"\d{2}", self.industry_code):
            raise StockSourceError("產業代碼無效")
        if self.issuer_id is not None and (not isinstance(self.issuer_id, str) or not re.fullmatch(r"\d{8}", self.issuer_id)):
            raise StockSourceError("統一編號格式無效")


@dataclass(frozen=True)
class StockSnapshot:
    market: str
    source_date: date
    records: tuple[StockRecord, ...]
    warnings: tuple[str, ...] = ()
    dataset: str = "company"
    source_date_kind: str = "official_report_date"

    @property
    def source_key(self):
        return self.market if self.dataset == "company" else self.market + ":ETF"

    @property
    def source_url(self):
        return (SOURCES if self.dataset == "company" else ETF_SOURCES)[self.market]

    def fingerprint(self):
        rows = [[r.code, r.name, str(r.listing_date) if r.listing_date else None, r.industry, r.industry_code, r.issuer_id, r.security_type]
                for r in sorted(self.records, key=lambda r: r.code)]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StockSourceError("官方 JSON 出現重複欄位")
        result[key] = value
    return result


def parse_snapshot(payload: str, market: str, today: date, *, minimum=500) -> StockSnapshot:
    if market not in SOURCES:
        raise StockSourceError("未知市場")
    try:
        rows = json.loads(payload, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise StockSourceError(f"{market} 並非有效 JSON") from error
    if not isinstance(rows, list) or not minimum <= len(rows) <= 20000:
        raise StockSourceError(f"{market} 名單為空或筆數異常，停止整批更新")
    schema = TWSE_FIELDS if market == "TWSE" else TPEX_FIELDS
    keys = ("出表日期", "公司代號", "公司簡稱", "上市日期", "產業別", "營利事業統一編號") if market == "TWSE" else (
        "Date", "SecuritiesCompanyCode", "CompanyAbbreviation", "DateOfListing", "SecuritiesIndustryCode", "UnifiedBusinessNo.")
    reports, codes, records, warnings = set(), set(), [], []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != schema:
            raise StockSourceError(f"{market} 第 {index} 列官方欄位已變更；停止整批更新")
        try:
            report, code, name, listing, industry, issuer = (row[key] for key in keys)
            validate_code(code)
            if code in codes:
                raise StockSourceError("同一市場股票代號重複")
            codes.add(code)
            reports.add(parse_official_date(report))
            listed = parse_official_date(listing, optional=True)
            if industry is not None and not isinstance(industry, str):
                raise StockSourceError("產業代碼不是字串")
            industry_code = industry.strip() or None if isinstance(industry, str) else None
            if industry_code and industry_code not in INDUSTRIES[market]:
                warnings.append(f"{code}: 未知產業代碼 {industry_code}，名稱保留 NULL")
            if listed is None:
                warnings.append(f"{code}: 官方掛牌日期缺少，不推測")
            record = StockRecord(code, name.strip() if isinstance(name, str) else name, market, listed,
                                 INDUSTRIES[market].get(industry_code), industry_code,
                                 issuer.strip() or None if isinstance(issuer, str) else issuer,
                                 "DR" if market == "TWSE" and industry_code == "91" else "STOCK")
            record.validate()
            records.append(record)
        except StockSourceError as error:
            raise StockSourceError(f"{market} 第 {index} 列：{error}") from error
    if len(reports) != 1:
        raise StockSourceError(f"{market} 出表日期不一致")
    source_date = reports.pop()
    if source_date > today or source_date < today - timedelta(days=14):
        raise StockSourceError(f"{market} 出表日期在未來或超過 14 日，保留舊資料")
    if not any(r.listing_date is not None and r.listing_date <= today for r in records):
        raise StockSourceError(f"{market} 整批皆缺少有效掛牌日，保留舊資料")
    return StockSnapshot(market, source_date, tuple(records), tuple(warnings))


def read_official(url: str) -> str:
    if url not in {*SOURCES.values(), *ETF_SOURCES.values()}:
        raise StockSourceError("不是已確認的官方來源")
    try:
        request = Request(url, headers={"User-Agent": "CapitalStockMaster/1.0", "Accept": "application/json", "Cache-Control": "no-cache"})
        if url == ETF_SOURCES["TPEx"]:
            # Official website performs an empty-body POST to retrieve its full list.
            request.data = b""
        with urlopen(request, timeout=25) as response:
            if response.geturl() != url:
                raise StockSourceError("官方來源重新導向，拒絕錯誤頁面")
            raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise StockSourceError("官方來源回應過大")
        return raw.decode("utf-8-sig")
    except (HTTPError, URLError, TimeoutError, UnicodeError, OSError) as error:
        reason = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
        raise StockSourceError(f"官方來源讀取失敗：{reason}") from error


def validate_snapshots(snapshots, today: date):
    keys = {(s.market, s.dataset) for s in snapshots}
    companies = {(market, "company") for market in SOURCES}
    full = companies | {(market, "etf") for market in ETF_SOURCES}
    if len(keys) != len(snapshots) or keys not in (companies, full):
        raise StockSourceError("公司及 ETF 各須兩市場皆成功，不允許部分市場更新")
    active_codes = set()
    all_keys = set()
    for snapshot in snapshots:
        if not snapshot.records or snapshot.source_date > today or snapshot.source_date < today - timedelta(days=14):
            raise StockSourceError("來源為空或日期異常")
        seen = set()
        for record in snapshot.records:
            record.validate()
            if record.market != snapshot.market or record.code in seen:
                raise StockSourceError("市場不符或代號重複")
            seen.add(record.code)
            if (record.market, record.code) in all_keys:
                raise StockSourceError("同一代號出現在多個商品資料集")
            all_keys.add((record.market, record.code))
            if (snapshot.dataset == "etf") != (record.security_type == "ETF"):
                raise StockSourceError("資料集與商品類型不符")
            if snapshot.source_date_kind != ("retrieval_date" if snapshot.dataset == "etf" else "official_report_date"):
                raise StockSourceError("來源日期語意不符")
            if record.listing_date is None or record.listing_date <= today:
                if record.code in active_codes:
                    raise StockSourceError("兩市場同時出現相同現行代號，須確認市場轉換後再更新")
                active_codes.add(record.code)


def fetch_snapshots(*, reader=read_official, now: datetime | None = None, minimum=500, include_etfs=True, etf_minimum=50):
    today = (now or datetime.now(timezone.utc)).astimezone(TAIPEI).date()
    result = []
    for market, url in SOURCES.items():
        try:
            result.append(parse_snapshot(reader(url), market, today, minimum=minimum))
        except StockSourceError as error:
            raise StockSourceError(f"{market}：{error}") from error
    if include_etfs:
        from app.services.etf_master_sources import parse_etf_snapshot
        for market, url in ETF_SOURCES.items():
            try:
                result.append(parse_etf_snapshot(reader(url), market, today, minimum=etf_minimum))
            except StockSourceError as error:
                raise StockSourceError(f"{market} ETF：{error}") from error
    validate_snapshots(result, today)
    return result
