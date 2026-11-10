"""Read only the currently effective version of each verified report."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json


TABLES = {
    "institutional_stock": ("institutional_stock_daily", ("stock_code", "stock_name"),
                            ("foreign_net_shares_excluding_foreign_dealers", "investment_trust_net_shares", "dealer_net_shares")),
    "institutional_market": ("institutional_market_daily", ("institution_type", "net_difference_flag", "net_difference_twd"),
                             ("buy_amount", "sell_amount", "net_amount")),
    "margin": ("margin_stock_daily", ("stock_code", "stock_name"),
               ("financing_previous_balance", "financing_balance", "short_previous_balance", "short_balance")),
    "margin_market": ("margin_market_daily", ("measure", "balance_unit"), ("previous_balance", "balance")),
    "revenue": ("revenue_monthly", ("stock_code", "stock_name", "consolidation_scope", "source_note"),
                ("revenue", "previous_month_revenue", "previous_year_same_month_revenue", "mom_percent",
                 "yoy_percent", "year_to_date_revenue", "previous_year_to_date_revenue", "year_to_date_yoy_percent")),
    "income": ("financial_statements", ("stock_code", "stock_name", "fiscal_year", "fiscal_quarter",
                                       "statement_type", "industry_schema", "consolidation_scope", "period_type"), ()),
    "balance": ("financial_statements", ("stock_code", "stock_name", "fiscal_year", "fiscal_quarter",
                                        "statement_type", "industry_schema", "consolidation_scope", "period_type"), ()),
}


def timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def amount(value):
    return format(value, "f") if isinstance(value, Decimal) else value


class MarketDataQueries:
    def __init__(self, connection, *, mysql=False):
        self.connection = connection
        self.mysql = mysql

    def select(self, sql, args):
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(sql.replace("?", "%s") if self.mysql else sql, args)
            return cursor.fetchall()

    def latest_revenue_month(self, stock_code, market):
        where_market = "AND s.market = ?" if market else ""
        return self.select(f"""
            SELECT MAX(s.period) FROM market_data_series s
            JOIN market_data_revenue_monthly d ON d.report_key = s.report_key AND d.revision = s.current_revision
            WHERE s.dataset = 'revenue' AND d.stock_code = ? {where_market}
        """, (stock_code, market) if market else (stock_code,))[0][0]

    def stock_market(self, stock_code):
        for dataset in ("institutional_stock", "margin", "revenue", "income", "balance"):
            table = TABLES[dataset][0]
            rows = self.select(f"""
                SELECT s.market, MAX(s.period) AS latest
                FROM market_data_series s
                JOIN market_data_{table} d ON d.report_key = s.report_key AND d.revision = s.current_revision
                WHERE s.dataset = ? AND d.stock_code = ?
                GROUP BY s.market ORDER BY latest DESC, s.market
            """, (dataset, stock_code))
            if rows:
                if len(rows) > 1 and rows[0][1] == rows[1][1]:
                    raise ValueError("ambiguous_market")
                return rows[0][0]
        return None

    def periods(self, dataset, market=None, *, stock_code=None, segment=None):
        table = TABLES[dataset][0]
        join = ""
        conditions = ["s.dataset = ?", "s.current_revision IS NOT NULL"]
        params = [dataset]
        if market is not None:
            conditions.append("s.market = ?")
            params.append(market)
        if stock_code is not None:
            join = f"JOIN market_data_{table} d ON d.report_key = s.report_key AND d.revision = s.current_revision"
            conditions.append("d.stock_code = ?")
            params.append(stock_code)
        if segment is not None:
            conditions.append("s.segment = ?")
            params.append(segment)
        rows = self.select(f"""
            SELECT DISTINCT s.period FROM market_data_series s {join}
            WHERE {' AND '.join(conditions)}
            ORDER BY s.period DESC LIMIT 128
        """, params)
        return [row[0] for row in rows]

    def current(self, dataset, start, end, *, stock_code=None, market=None, segment=None, page=(50, 0)):
        limit, offset = page
        table, identity, fields = TABLES[dataset]
        extra_fields = ", ".join(f"d.{column}" for column in (*identity, *fields))
        conditions = ["s.dataset = ?", "s.period BETWEEN ? AND ?"]
        params = [dataset, start, end]
        for value, column in ((stock_code, "d.stock_code"), (market, "s.market"), (segment, "s.segment")):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        rows = self.select(f"""
            SELECT s.market, s.period, s.segment, s.report_key, s.current_revision, s.source_id,
                p.source_metadata, p.published_at, p.available_at, p.imported_at,
                CASE WHEN c.coverage_verified = 1 AND c.accepted_raw_sha256 = p.raw_sha256 THEN 1 ELSE 0 END,
                {extra_fields}
            FROM market_data_series s
            JOIN market_data_snapshots p ON p.report_key = s.report_key AND p.revision = s.current_revision
            JOIN market_data_{table} d ON d.report_key = s.report_key AND d.revision = s.current_revision
            LEFT JOIN market_data_checkpoints c ON c.report_key = s.report_key
            WHERE {' AND '.join(conditions)}
            ORDER BY s.period DESC, s.market, s.segment, d.{identity[0]}
            LIMIT ? OFFSET ?
        """, (*params, limit + 1, offset))
        data = []
        for raw in rows[:limit]:
            (market, period, scope, report_key, revision, source_id, source_metadata,
             published_at, available_at, updated_at, complete, *columns) = raw
            meta = json.loads(source_metadata)
            item = dict(zip(identity, columns[:len(identity)]))
            item.update(market=market, period=period, scope=scope or None, revision=revision,
                        source={"id": source_id, "url": meta["source"]["source_url"]},
                        unit=meta["units"], published_at=timestamp(published_at), available_at=timestamp(available_at),
                        last_updated_at=timestamp(updated_at), is_complete=bool(complete))
            if dataset == "institutional_market":
                item["net_difference_flag"] = bool(item["net_difference_flag"])
                item["net_difference_twd"] = amount(item["net_difference_twd"])
            if dataset == "margin_market":
                item["unit"] = dict.fromkeys(fields, item.pop("balance_unit"))
            if dataset in ("income", "balance"):
                facts = self.select("""
                    SELECT source_label, amount, unit FROM market_data_financial_facts
                    WHERE report_key = ? AND revision = ? AND stock_code = ? ORDER BY source_label
                """, (report_key, revision, stock_code))
                item["values"] = {label: amount(value) for label, value, _ in facts}
                item["unit"] = {label: unit for label, _, unit in facts}
            else:
                item["values"] = {field: amount(value) for field, value in zip(fields, columns[len(identity):])}
            data.append(item)
        units = data[0]["unit"] if data and all(item["unit"] == data[0]["unit"] for item in data) else None
        publication_times = {item["published_at"] for item in data}
        return {
            "data": data, "period": {"start": start, "end": end}, "unit": units,
            "source": list({item["source"]["id"]: item["source"] for item in data}.values()),
            "published_at": publication_times.pop() if len(publication_times) == 1 else None,
            "last_updated_at": max((item["last_updated_at"] for item in data), default=None),
            "is_complete": bool(data) and all(item["is_complete"] for item in data),
            "limit": limit, "offset": offset, "next_offset": offset + limit if len(rows) > limit and offset + limit <= 5000 else None,
        }

    def status(self, *, page=(50, 0)):
        limit, offset = page
        rows = self.select("""
            SELECT c.dataset, c.market, c.period, c.segment, c.last_status,
                c.last_attempt_at, c.last_success_at, s.current_revision,
                CASE WHEN c.coverage_verified = 1 AND c.accepted_raw_sha256 = p.raw_sha256 THEN 1 ELSE 0 END
            FROM market_data_checkpoints c
            LEFT JOIN market_data_series s ON s.report_key = c.report_key
            LEFT JOIN market_data_snapshots p ON p.report_key = s.report_key AND p.revision = s.current_revision
            ORDER BY c.period DESC, c.dataset, c.market, c.segment
            LIMIT ? OFFSET ?
        """, (limit + 1, offset))
        return {
            "data": [
                {"dataset": dataset, "market": market, "period": period, "scope": segment or None,
                 "status": state, "last_attempt_at": timestamp(attempted), "last_success_at": timestamp(succeeded),
                 "revision": revision, "is_complete": bool(complete)}
                for dataset, market, period, segment, state, attempted, succeeded, revision, complete in rows[:limit]
            ],
            "limit": limit, "offset": offset, "next_offset": offset + limit if len(rows) > limit and offset + limit <= 5000 else None,
        }
