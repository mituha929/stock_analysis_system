"""Replay a bounded official cache into an isolated SQLite test database."""
import argparse
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import re

from scripts.collect_market_history import (
    DEFAULT_OUTPUT, MARGIN_BALANCE_FIELDS, TAIPEI, Reader, chip_jobs, daily_jobs, decode, financial_jobs,
    inspect, margin_balance, number, revenue_jobs, save_json,
)
from scripts.market_data_quality import MARKET_NET_TOLERANCE_TWD, market_net_difference

TEST_OUTPUT = DEFAULT_OUTPUT.parent
STAGING_DIR = TEST_OUTPUT / "market_data_p1"
LATEST_DAY = "2026-11-04"
NET_EXCEPTION_DAY = "2026-10-07"
SAMPLES = {
    "institutional_stock": ("M01", "2330", "6488"),
    "institutional_market": ("M02", None, None),
    "margin": ("M03", "2330", "6488"),
    "revenue": ("M04", "2330", "6488"),
    "income": ("M05", "1215", "1584"),
    "balance": ("M06", "1215", "1584"),
}
REQUIRED_SAMPLE_VALUES = {
    "revenue": ("revenue",),
    "income": ("營業收入", "本期淨利（淨損）"),
    "balance": ("資產總計", "負債總計"),
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS staging_reports (
    report_key TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    dataset TEXT NOT NULL,
    market TEXT NOT NULL,
    period TEXT NOT NULL,
    segment TEXT NOT NULL,
    market_scope TEXT,
    raw_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (report_key, raw_sha256)
);
CREATE TABLE IF NOT EXISTS staging_records (
    report_key TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    record_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    net_difference_flag INTEGER NOT NULL CHECK (net_difference_flag IN (0, 1)),
    net_difference_twd TEXT,
    PRIMARY KEY (report_key, raw_sha256, record_key),
    FOREIGN KEY (report_key, raw_sha256) REFERENCES staging_reports (report_key, raw_sha256)
);
"""


def pilot_jobs():
    yield from daily_jobs(LATEST_DAY)
    yield from revenue_jobs("2026-09")
    yield from financial_jobs("2026-Q3")
    for job in daily_jobs(NET_EXCEPTION_DAY):
        if job.dataset == "institutional_market" and job.market == "TPEx":
            yield job


def load_report(reader, job):
    source, raw = reader.read(job)
    return verify_report(reader, job, source, raw)


def verify_report(reader, job, source, raw):
    if source.get("http_status") != 200 or source.get("error"):
        raise ValueError(source.get("error") or "source_http_status")
    if (
        source.get("source_url") != job.url
        or source.get("parameters") != job.parameters
        or not raw
        or hashlib.sha256(raw).hexdigest() != source.get("sha256")
    ):
        raise ValueError("source_identity_or_hash_mismatch")
    parsed = (verify_tpex_margin_amount(reader, job, source, raw)
              if job.dataset == "margin_market" and job.segment == "amount" else inspect(job, source, raw))
    if parsed["status"] != "verified":
        raise ValueError(parsed.get("reason") or "source_not_verified")
    raw_path = reader.folder / "raw" / f"{source['cache_key']}.bin"
    return parsed, raw_path


def verify_tpex_margin_amount(reader, job, source, raw):
    day = date.fromisoformat(job.period)
    observed = datetime.fromisoformat(source["fetched_at"])
    # The board only carries MM/DD. Use a recent date plus two exact matches
    # against the official, fully dated security report before assigning a year.
    if observed.utcoffset() is None or not 0 <= (observed.astimezone(TAIPEI).date() - day).days <= 75:
        raise ValueError("tpex_market_margin_outside_datable_window")
    board = json.loads(decode(raw, source.get("content_type", "")))

    def balance(field):
        values = []
        for point in board[field]:
            label = re.fullmatch(r"(\d{1,2})/(\d{1,2})", point["date"])
            if label and tuple(map(int, label.groups())) == (day.month, day.day):
                values.append(number(point["count"]))
        if len(values) != 1 or values[0] is None or not values[0].isdecimal():
            raise ValueError("tpex_market_margin_date_or_value_unverified")
        return values[0]

    reference = next(item for item in chip_jobs(job.period)
                     if item.market == "TPEx" and item.dataset == "margin_market")
    detail, path = load_report(reader, reference)
    detail_balances = {row["measure"]: row["values"]["balance"] for row in detail["records"]}
    if (balance("marginPurchaseShares"), balance("marginShortSaleShares")) != (
        detail_balances["financing_lots"], detail_balances["short_lots"],
    ):
        raise ValueError("tpex_market_margin_date_crosscheck_failed")
    measure = "financing_amount"
    record = margin_balance(measure, None, balance("marginPurchaseValue"), "TWD_thousand")
    return {
        "status": "verified", "dataset": job.dataset, "market": job.market, "period": job.period,
        "segment": job.segment, "record_count": 1, "records": [record], "units": {measure: record["unit"]},
        "source": source | {"date_reference": {"url": reference.url, "sha256": detail["source"]["sha256"],
                                               "period": job.period, "file": path.name}},
        "period_provenance": "board.month_day + matching financing and short balances in dated TPEx report",
        "value_basis": "published_market_balance",
    }


def record_key(record):
    return record.get("stock_code") or record.get("institution_type") or record.get("measure")


def validated_rows(report):
    identifiers = set()
    for record in report["records"]:
        identifier = record_key(record)
        if not identifier or identifier in identifiers:
            raise ValueError(f"invalid_or_duplicate_record_key: {identifier}")
        identifiers.add(identifier)
        values = record["values"]
        if report["dataset"] == "margin_market":
            if (set(values) != set(MARGIN_BALANCE_FIELDS) or values["balance"] is None
                    or report["units"].get(identifier) != record.get("unit")):
                raise ValueError(f"missing_fields_or_units: {identifier}")
        elif not values or values.keys() - report["units"].keys():
            raise ValueError(f"missing_fields_or_units: {identifier}")
        difference = None
        if report["dataset"] == "institutional_market":
            difference = market_net_difference(values)
            if abs(difference) > MARKET_NET_TOLERANCE_TWD:
                raise ValueError(f"market_net_outside_tolerance: {identifier}: {difference} TWD")
        elif report["dataset"] in {"margin", "margin_market"}:
            if any(Decimal(value) < 0 for value in values.values() if value is not None):
                raise ValueError(f"negative_margin_balance: {identifier}")
        elif report["dataset"] == "balance":
            assets, liabilities, equity = (values.get(key) for key in ("資產總計", "負債總計", "權益總計"))
            if None not in (assets, liabilities, equity) and abs(
                Decimal(assets) - Decimal(liabilities) - Decimal(equity)
            ) > 2:
                raise ValueError(f"balance_equation_mismatch: {identifier}")
        yield identifier, record, int(bool(difference)), None if difference is None else str(difference)


def stage_report(connection, job, report, raw_path):
    rows = list(validated_rows(report))
    if not rows:
        raise ValueError("empty_staging_batch")
    digest = report["source"]["sha256"]
    metadata = {key: report[key] for key in (
        "source", "units", "source_fields", "period_provenance", "value_basis", "latest_quarter_partial",
    ) if key in report}
    metadata["published_at"] = None
    metadata["revision"] = None
    with connection:
        inserted = connection.execute("""
            INSERT INTO staging_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_key, raw_sha256) DO NOTHING
        """, (job.key, digest, job.dataset, job.market, job.period, job.segment,
              report.get("market_scope"), str(raw_path.resolve()), json.dumps(metadata, ensure_ascii=False)))
        if inserted.rowcount:
            connection.executemany("""
                INSERT INTO staging_records VALUES (?, ?, ?, ?, ?, ?)
            """, ((job.key, digest, key, json.dumps(record, ensure_ascii=False), flag, difference)
                  for key, record, flag, difference in rows))
    return {"rows": len(rows), "flagged_rows": sum(row[2] for row in rows),
            "inserted": bool(inserted.rowcount), "raw_sha256": digest}


def representative_sample(job, report, raw_path):
    if job.period == NET_EXCEPTION_DAY or job.segment == "1":
        return None
    if job.dataset == "institutional_market" and job.market == "TPEx" and job.segment != "stocks":
        return None
    test_id, twse_code, tpex_code = SAMPLES[job.dataset]
    code = twse_code if job.market == "TWSE" else tpex_code
    record = next(row for row in report["records"] if (
        row.get("stock_code") == code if code else row.get("institution_type") == "投信"
    ))
    required = REQUIRED_SAMPLE_VALUES.get(job.dataset, tuple(report["units"]))
    if any(record["values"].get(key) is None for key in required):
        raise ValueError("representative_sample_missing_required_values")
    sample = {
        "test_id": test_id, "dataset": job.dataset, "market": job.market, "period": job.period,
        "segment": job.segment, "market_scope": report.get("market_scope"),
        "period_provenance": report["period_provenance"], "value_basis": report.get("value_basis"),
        "published_at": None, "revision": None, **record, "units": report["units"],
        "source_fields": report["source_fields"],
        "source": report["source"], "raw_path": raw_path.relative_to(TEST_OUTPUT).as_posix(),
    }
    if job.dataset == "institutional_market":
        difference = market_net_difference(record["values"])
        sample.update(net_difference_flag=bool(difference), net_difference_twd=str(difference))
    return f"{test_id}_{job.market}.json", sample


def run():
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    samples_dir = TEST_OUTPUT / "samples"
    samples_dir.mkdir(exist_ok=True)
    reader = Reader(DEFAULT_OUTPUT, offline=True)
    results = []
    with closing(sqlite3.connect(STAGING_DIR / "staging.sqlite3")) as connection:
        connection.executescript(SCHEMA)
        for job in pilot_jobs():
            try:
                report, raw_path = load_report(reader, job)
                sample = representative_sample(job, report, raw_path)
                result = stage_report(connection, job, report, raw_path)
                if sample:
                    name, content = sample
                    save_json(samples_dir / name, content)
                    result["sample"] = name
            except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
                result = {"error": str(exc) or type(exc).__name__}
            results.append({"report_key": job.key, **result})
    save_json(STAGING_DIR / "results.json", results)
    return results


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    results = run()
    print(json.dumps({
        "reports": len(results),
        "errors": sum("error" in result for result in results),
        "new_reports": sum(result.get("inserted", False) for result in results),
        "rows": sum(result.get("rows", 0) for result in results),
        "flagged_rows": sum(result.get("flagged_rows", 0) for result in results),
        "samples": sum("sample" in result for result in results),
    }))
    raise SystemExit(1 if any("error" in result for result in results) else 0)
