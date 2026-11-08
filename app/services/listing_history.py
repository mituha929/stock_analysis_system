"""Conservative listing limits, never a reason to rewrite existing history.

TWSE's listing date can be a board transfer. Only an exact official admission
record which is not a transfer can certify that date as a history boundary.
Unknown/transfer dates fall back to the old search, rather than hiding OTC bars.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import json
import logging
import re
import unicodedata
from urllib.request import Request, urlopen

from app.services.backfill_policy import period_start
from app.services.day_candle_integrity import official_reference
from app.services.stock_master_sources import parse_official_date, TPEX_FIELDS

logger = logging.getLogger(__name__)
ORIGINS_URL = "https://openapi.twse.com.tw/v1/company/newlisting"
ESB_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R"
MASTER_COLUMNS = {"code", "market", "listing_date", "issuer_id", "security_type",
                  "presence_status", "date_status", "source_url"}


def parse_listing_origins(rows):
    """Absent/mismatched records are UNKNOWN, never evidence of a direct IPO."""
    if not isinstance(rows, list) or not rows or len(rows) > 20000:
        raise ValueError("上市來源清單格式不符")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not {"Code", "ApprovedListingDate", "Note"} <= row.keys():
            raise ValueError("上市來源欄位變動")
        if not all(isinstance(row[k], str) for k in ("Code", "ApprovedListingDate", "Note")):
            raise ValueError("上市來源欄位型別不符")
        listed = parse_official_date(row["ApprovedListingDate"], optional=True)
        if listed is None:
            continue
        note = row["Note"].strip()
        # Reorganizations/conversions cannot establish the first trading date
        # either. A missing predecessor date must not truncate its history.
        kind = "transfer" if any(s in note for s in ("櫃轉市", "上櫃轉上市")) else "unknown" if "轉" in note else "direct"
        key = (row["Code"], listed)
        if key in result and result[key] != kind:
            raise ValueError("上市來源紀錄互相矛盾")
        result[key] = kind
    return result


@lru_cache(maxsize=1)
def fetch_twse_listing_origins(observed_on):
    """One bounded, read-only official request per day/process, outside SQL TX.

    A source outage only disables TWSE-stock boundary optimization. It never
    blocks quotes, fabricates an IPO date, or changes the profile database.
    """
    try:
        request = Request(ORIGINS_URL, headers={"User-Agent": "capital-api/listing-boundary", "Accept": "application/json"})
        with urlopen(request, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("上市來源回應過大")
        return parse_listing_origins(json.loads(raw.decode("utf-8-sig")))
    except Exception as error:
        logger.warning("上市來源尚未核實，保留轉上市前歷史探查：%s", type(error).__name__)
        return {}


def reliable(row, today, *, current=True):
    return (type(row.get("listing_date")) is date and row["listing_date"] <= today
            and row.get("date_status") in {"provided", "retained_previous_official"}
            and row.get("security_type") in {"STOCK", "DR", "ETF"}
            and row.get("market") in {"TWSE", "TPEx", "ESB"}
            and (not current or row.get("presence_status") == "present")
            and official_reference(row.get("source_url")))


def parse_esb_listing_profiles(rows, observed_on):
    """Official ESB admission dates, never incorporation dates or guessed aliases.

    The current stock_master schema remains TWSE/TPEx-only. A bounded official
    ESB snapshot supplies optional limits without writing another master table.
    One malformed/changed schema disables the entire snapshot, not selected rows.
    """
    today = date.fromisoformat(observed_on)
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("興櫃基本資料不是有效的完整清單")
    result, codes = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != TPEX_FIELDS:
            raise ValueError("興櫃基本資料欄位變動")
        code, name = row['SecuritiesCompanyCode'], row['CompanyAbbreviation']
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{4,6}', code)
                or code in codes or not isinstance(name, str) or not name.strip()):
            raise ValueError("興櫃代碼或名稱不合法／重複")
        codes.add(code)
        source_date = parse_official_date(row['Date'])
        if source_date > today:
            raise ValueError("興櫃資料日期晚於核對日期")
        listed = parse_official_date(row['DateOfListing'], optional=True)
        # A future admission is not yet evidence limiting historical data.
        if listed is None or listed > min(today, source_date):
            continue
        issuer = row['UnifiedBusinessNo.']
        issuer = issuer if isinstance(issuer, str) and re.fullmatch(r'[0-9]{8}', issuer) else None
        result.append(dict(code=code, name=name.strip(), market='ESB', listing_date=listed,
                           issuer_id=issuer, security_type='STOCK', presence_status='present',
                           date_status='provided', source_url=ESB_URL))
    return tuple(result)


@lru_cache(maxsize=1)
def fetch_esb_listing_profiles(observed_on):
    """Once per day/process, outside SQL transactions; failure preserves history."""
    try:
        request = Request(ESB_URL, headers={'User-Agent': 'capital-api/listing-boundary', 'Accept': 'application/json'})
        with urlopen(request, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('興櫃基本資料回應過大')
        return parse_esb_listing_profiles(json.loads(raw.decode('utf-8-sig')), observed_on)
    except Exception as error:
        logger.warning('興櫃日期尚未核實，不限制或刪除歷史：%s', type(error).__name__)
        return ()


def _normalized_name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def listing_policy(row, profiles, older_history=False, origins=None, today=None):
    today = today or date.today()
    if not reliable(row, today):
        return None
    listed = row["listing_date"]
    boundary, reason, source = listed, ("official_esb_listing" if row['market'] == 'ESB' else "official_listing"), row["source_url"]
    # A predecessor is usable only with a matching issuer, not merely a reused
    # stock code or similar company name. The old market row may be inactive.
    previous = [p for p in profiles if p.get("code") == row["code"]
                and p.get("market") != row["market"] and reliable(p, today, current=False)
                and row.get("issuer_id") and p.get("issuer_id") == row["issuer_id"]
                and p.get("security_type") == row["security_type"]
                and p["listing_date"] < listed]
    if previous:
        predecessor = min(previous, key=lambda p: p["listing_date"])
        boundary, source = predecessor["listing_date"], predecessor["source_url"]
        reason = "official_predecessor_listing"
    elif row["market"] == "TWSE" and row["security_type"] == "STOCK":
        kind = (origins or {}).get((row["code"], listed))
        if kind != "direct":
            boundary = None
            reason = "transfer_date_unknown" if kind == "transfer" else "listing_origin_unverified"
    if older_history:
        boundary, reason = None, "existing_history_precedes_listing"
    return dict(listing_date=listed, history_start=boundary, reason=reason,
                source=source, listing_source=row["source_url"], market=row["market"],
                origin_source=ORIGINS_URL if row["market"] == "TWSE" and row["security_type"] == "STOCK" else None)


def listing_floor(policy, interval):
    boundary = (policy or {}).get("history_start")
    return period_start(interval, boundary) if boundary else None


class ListingHistoryRepository:
    def load_listing_policies(self, candle_table, market):
        policies = self._load_stock_listing_policies(candle_table, market)
        if getattr(self, '_lifecycle_ready', False):
            policies.update(self.bond_lifecycle_policies(market))
            if getattr(self, '_empty_review_ready', False):
                self.reconcile_lifecycle_reviews(candle_table, market, policies)
        return policies

    def _load_stock_listing_policies(self, candle_table, market):
        official_market = {0: "TWSE", 1: "TPEx", 4: "ESB", 5: "TWSE", 6: "TPEx"}.get(market)
        if official_market is None:
            return {}
        table = self._validate_table_name(candle_table)
        today = date.today()
        esb = fetch_esb_listing_profiles(today.isoformat()) if market == 4 else ()
        if market == 4 and not esb:
            return {}
        with self.transaction(dictionary=True) as q:
            if market == 4:
                q.execute("SELECT symbol,name FROM instruments WHERE market_no=4 AND is_active=TRUE")
                names = {str(r['symbol']): _normalized_name(r['name']) for r in q.fetchall()}
                profiles = [r for r in esb if _normalized_name(r['name']) == names.get(r['code'])]
            else:
                q.execute("SELECT COLUMN_NAME FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='stock_master'")
                if not MASTER_COLUMNS <= {r["COLUMN_NAME"] for r in q.fetchall()}:
                    return {}  # Optional master not installed/compatible: old behavior.
                q.execute("SELECT code,market,listing_date,issuer_id,security_type,presence_status,date_status,source_url FROM stock_master")
                profiles = q.fetchall()
            by_code = {}
            for row in profiles:
                by_code.setdefault(row['code'], []).append(row)
            selected = [r for r in profiles if r['market'] == official_market and reliable(r, today)]
            candidates = {r['code']: listing_policy(r, by_code[r['code']], today=today) for r in selected}
            # Loose index GROUP MIN reads one timestamp per symbol/interval,
            # not millions of candle rows through correlated date predicates.
            # Period-start labels inside the listing period are not conflicts.
            dates = {r['code']: candidates[r['code']]['history_start'] or r['listing_date'] for r in selected}
            older = set()
            if dates:
                marks = ','.join(['%s']*len(dates))
                q.execute(f"SELECT symbol,interval_code,MIN(candle_time) AS earliest FROM `{table}` "
                          f"WHERE symbol IN({marks}) GROUP BY symbol,interval_code", tuple(dates))
                for row in q.fetchall():
                    if row['earliest'] is not None and row['interval_code'] in {'day','1m','5m','15m','60m','week','month'}:
                        oldest = row['earliest'].date() if hasattr(row['earliest'], 'date') else row['earliest']
                        if oldest < period_start(row['interval_code'], dates[row['symbol']]):
                            older.add(row['symbol'])
        origins = (fetch_twse_listing_origins(today.isoformat()) if any(
            candidates[r['code']]['reason'] == 'listing_origin_unverified' and r['code'] not in older for r in selected) else {})
        return {r["code"]: listing_policy(r, by_code[r['code']], r["code"] in older, origins, today) for r in selected}
