"""Transactional report versions for MySQL, with the same contract in SQLite tests."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from urllib.parse import urlsplit

from scripts.collect_market_history import MARGIN_BALANCE_FIELDS, PROJECT
from scripts.stage_market_data import validated_rows

MYSQL_DDL = PROJECT / "sql" / "market_data_p2_mysql.sql"
PREFIX = "market_data_"
STOCK_FIELDS = {
    "institutional_stock": (
        "institutional_stock_daily", (
            "foreign_net_shares_excluding_foreign_dealers", "investment_trust_net_shares", "dealer_net_shares",
        ),
    ),
    "margin": (
        "margin_stock_daily", (
            "financing_previous_balance", "financing_balance", "short_previous_balance", "short_balance",
        ),
    ),
    "revenue": (
        "revenue_monthly", (
            "revenue", "previous_month_revenue", "previous_year_same_month_revenue",
            "mom_percent", "yoy_percent", "year_to_date_revenue",
            "previous_year_to_date_revenue", "year_to_date_yoy_percent",
        ),
    ),
}
MARKET_FIELDS = ("buy_amount", "sell_amount", "net_amount")


def sqlite_schema(path=MYSQL_DDL):
    return (
        "PRAGMA foreign_keys = ON;\n"
        + path.read_text(encoding="utf-8")
        .replace(" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin", "")
        .replace("DECIMAL(38,12)", "TEXT")
    )


def utc_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("source_timestamp_requires_timezone")
    return timestamp.astimezone(timezone.utc).replace(tzinfo=None)


def exact_decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("invalid_decimal_value")
        if not number:
            return "0"
        parts = number.as_tuple()
        digits = "".join(map(str, parts.digits))
        scale = max(0, -parts.exponent - (len(digits) - len(digits.rstrip("0"))))
        if number.adjusted() >= 26 or scale > 12:
            raise ValueError("value_exceeds_decimal_38_12")
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid_decimal_value") from exc
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def market_segment(job):
    if job.dataset == "institutional_market":
        if job.market == "TPEx":
            return "stocks" if job.segment == "stocks" else "securities"
        return "published"
    return job.segment


def prepare_rows(job, report):
    """Return explicit columns, business rows and financial facts for one report."""
    if any(report.get(field) != getattr(job, field) for field in ("dataset", "market", "period", "segment")):
        raise ValueError("report_identity_mismatch")
    dataset = job.dataset
    validated = list(validated_rows(report))
    if not validated:
        raise ValueError("empty_batch")
    rows, facts = [], []
    flagged = sum(row[2] for row in validated)
    if dataset == "institutional_market":
        table = "institutional_market_daily"
        columns = ("institution_type", *MARKET_FIELDS, "net_difference_flag", "net_difference_twd")
        for key, record, flag, difference in validated:
            rows.append((key, *(exact_decimal(record["values"][name]) for name in MARKET_FIELDS),
                         flag, exact_decimal(difference)))
    elif dataset == "margin_market":
        table = "margin_market_daily"
        columns = ("measure", "balance_unit", *MARGIN_BALANCE_FIELDS)
        for key, record, _, _ in validated:
            rows.append((key, record["unit"], *(exact_decimal(record["values"][name]) for name in MARGIN_BALANCE_FIELDS)))
    elif dataset in STOCK_FIELDS:
        table, fields = STOCK_FIELDS[dataset]
        columns = ("stock_code", "stock_name", *fields)
        if dataset == "revenue":
            columns += ("consolidation_scope", "source_note")
        for key, record, _, _ in validated:
            if set(record["values"]) != set(fields):
                raise ValueError("unexpected_stock_value_fields")
            row = (key, record["name"], *(exact_decimal(record["values"][name]) for name in fields))
            rows.append(row + (None, record.get("note")) if dataset == "revenue" else row)
    elif dataset in ("income", "balance"):
        table = "financial_statements"
        columns = ("stock_code", "stock_name", "fiscal_year", "fiscal_quarter", "statement_type",
                   "industry_schema", "consolidation_scope", "period_type")
        statement, basis = ("income_statement", "year_to_date") if dataset == "income" else (
            "balance_sheet", "point_in_time"
        )
        year, quarter = map(int, job.period.split("-Q"))
        if report["value_basis"] != basis:
            raise ValueError("unexpected_financial_period_type")
        for key, record, _, _ in validated:
            rows.append((key, record["name"], year, quarter, statement, "ci", None, basis))
            for label, amount in record["values"].items():
                facts.append((key, label, None, exact_decimal(amount), report["units"][label]))
    else:
        raise ValueError("unsupported_dataset")
    return table, columns, rows, facts, flagged


class MarketDataStore:
    def __init__(self, connection, *, mysql=False):
        self.connection = connection
        self.mysql = mysql

    def execute(self, cursor, sql, args=()):
        cursor.execute(sql.replace("?", "%s") if self.mysql else sql, args)

    def insert_rows(self, cursor, table, columns, rows):
        mark = "%s" if self.mysql else "?"
        placeholders = ", ".join([mark] * len(columns))
        cursor.executemany(f"INSERT INTO {PREFIX}{table} ({', '.join(columns)}) VALUES ({placeholders})", rows)

    def begin(self):
        if self.mysql:
            self.connection.start_transaction(isolation_level="READ COMMITTED")
        else:
            self.connection.execute("BEGIN IMMEDIATE")

    def insert_identity(self, cursor, table, columns, values):
        key = columns[0]
        placeholders = ", ".join(["?"] * len(columns))
        conflict = f"ON DUPLICATE KEY UPDATE {key} = {key}" if self.mysql else f"ON CONFLICT ({key}) DO NOTHING"
        self.execute(cursor, f"INSERT INTO {PREFIX}{table} ({', '.join(columns)}) VALUES ({placeholders}) {conflict}",
                     values)

    def import_report(self, job, report, raw_path):
        table, columns, rows, facts, flagged = prepare_rows(job, report)
        source = report["source"]
        seen = utc_time(source["fetched_at"])
        digest = source["sha256"]
        segment = market_segment(job)
        source_id = ":".join((job.dataset, job.market, segment))
        url = urlsplit(job.url)
        origin = f"{url.scheme}://{url.netloc}"
        identity = (source_id, job.dataset, job.market, job.period, segment)
        metadata = {key: report[key] for key in (
            "source", "units", "source_fields", "period_provenance", "value_basis", "latest_quarter_partial",
        ) if key in report}
        summary = {"rows": len(rows), "facts": len(facts), "flagged_rows": flagged}
        self.begin()
        try:
            with closing(self.connection.cursor()) as cursor:
                self.insert_identity(cursor, "sources", ("source_id", "origin"), (source_id, origin))
                self.insert_identity(cursor, "series", ("report_key", "source_id", "dataset", "market",
                                                         "period", "segment"), (job.key, *identity))
                self.execute(cursor, """
                    SELECT source_id, dataset, market, period, segment, current_revision
                    FROM market_data_series WHERE report_key = ?
                """ + (" FOR UPDATE" if self.mysql else ""), (job.key,))
                series = cursor.fetchone()
                if series is None or tuple(series[:5]) != identity:
                    raise ValueError("stored_report_identity_mismatch")
                current = series[5]
                self.execute(cursor, """
                    SELECT revision FROM market_data_snapshots WHERE report_key = ? AND raw_sha256 = ?
                """, (job.key, digest))
                duplicate = cursor.fetchone()
                if duplicate:
                    result = dict(summary, status="unchanged", revision=duplicate[0], is_current=duplicate[0] == current)
                else:
                    result = self.insert_snapshot(cursor, job, raw_path, table, columns, rows, facts,
                                                  summary, metadata, digest, seen, current)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return result

    def insert_snapshot(self, cursor, job, raw_path, table, columns, rows, facts,
                        summary, metadata, digest, seen, current):
        promote = current is None
        if current is not None:
            self.execute(cursor, """
                SELECT first_seen_at FROM market_data_snapshots WHERE report_key = ? AND revision = ?
            """, (job.key, current))
            (active_seen,) = cursor.fetchone()
            if isinstance(active_seen, str):
                active_seen = datetime.fromisoformat(active_seen)
            if seen == active_seen:
                raise ValueError("different_snapshots_at_same_observation_time")
            promote = seen > active_seen
            if promote:
                self.execute(cursor, f"SELECT {columns[0]} FROM {PREFIX}{table} WHERE report_key = ? AND revision = ?",
                             (job.key, current))
                prior_keys = {row[0] for row in cursor.fetchall()}
                missing = prior_keys - {row[0] for row in rows}
                if missing:
                    raise ValueError(f"coverage_decreased: {len(missing)} previously present rows")
        self.execute(cursor, "SELECT COALESCE(MAX(revision), 0) FROM market_data_snapshots WHERE report_key = ?",
                     (job.key,))
        revision = cursor.fetchone()[0] + 1
        self.execute(cursor, """
            INSERT INTO market_data_snapshots
            (report_key, revision, raw_sha256, raw_path, source_metadata, first_seen_at, imported_at, row_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job.key, revision, digest, str(Path(raw_path).resolve()), json.dumps(metadata, ensure_ascii=False),
              seen.isoformat(sep=" "), datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "), len(rows)))
        self.insert_rows(cursor, table, ("report_key", "revision", *columns),
                         [(job.key, revision, *row) for row in rows])
        if facts:
            self.insert_rows(cursor, "financial_facts",
                             ("report_key", "revision", "stock_code", "source_label", "standard_code", "amount", "unit"),
                             [(job.key, revision, *row) for row in facts])
        if promote:
            self.execute(cursor, "UPDATE market_data_series SET current_revision = ? WHERE report_key = ?",
                         (revision, job.key))
        return dict(summary, status="promoted" if promote else "archived", revision=revision, is_current=promote)

    def log_result(self, run_id, result):
        self.begin()
        try:
            with closing(self.connection.cursor()) as cursor:
                self.execute(cursor, """
                    INSERT INTO market_data_ingestion_runs
                    (run_id, report_key, raw_sha256, finished_at, status, row_count, flagged_rows, error_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (run_id, result["report_key"], result.get("raw_sha256"),
                      datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "), result["status"],
                      result.get("rows", 0), result.get("flagged_rows", 0), result.get("error", "")[:255] or None))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
