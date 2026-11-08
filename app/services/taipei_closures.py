"""Standalone Taipei disaster closures; deliberately NOT a trading calendar.

The official annual CSV and administrator additions are separate files. Nothing
in this module starts Capital, touches MySQL, or changes backfill scheduling.
Unknown announcement wording is retained for review, never treated as a full
city closure. Absence from this data is not proof that a day was a trading day.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parents[2]
DATASET_URL = "https://data.taipei/dataset/detail?id=83b013c2-35f3-4470-98d7-03dd68a372cb"
DOWNLOAD_URL = "https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=7a0b00b7-988e-4a45-9f4b-c1f910fea57c"
SNAPSHOT_PATH = PROJECT / "data" / "taipei_closures_official.json"
MANUAL_PATH = PROJECT / "config" / "taipei_closures_manual.json"
MERGED_PATH = PROJECT / "data" / "taipei_closures.json"
REPORT_PATH = PROJECT / "docs" / "TAIPEI_CLOSURES_DATES.md"
TAIPEI_TZ = timezone(timedelta(hours=8))
HEADERS = ("民國年", "月", "日", "天然災害名稱", "臺北市停止上班上課情形")
SCOPES = {"citywide": "全市", "partial_area": "局部地區（可能含學校）",
          "specific_schools": "指定學校", "review": "待人工確認", "none": "未停班停課"}
PERIODS = {"full_day": "全天", "morning": "上午", "afternoon": "下午",
           "evening": "晚間", "partial": "部分／混合時段（見公告）", "unknown": "待確認"}
LIMIT_BYTES = 2 * 1024 * 1024


class TaipeiClosureError(ValueError):
    """Invalid public data or administrator configuration; keep prior files."""


def taipei_today() -> date:
    return datetime.now(TAIPEI_TZ).date()


def window_start(as_of: date, years: int = 10) -> date:
    if not 1 <= years <= 100:
        raise TaipeiClosureError("years 必須介於 1 與 100")
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:  # Leap day -> last valid day in February.
        return as_of.replace(year=as_of.year - years, day=28)


def iso_date(value: object) -> date:
    try:
        if not isinstance(value, str):
            raise ValueError("date must be a string")
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("use YYYY-MM-DD")
        return parsed
    except ValueError as error:
        raise TaipeiClosureError(f"日期無效，請使用 YYYY-MM-DD：{value!r}") from error


def classify(description: str) -> dict:
    text = re.sub(r"\s+", "", description).replace("，", "、")
    body = re.sub(r"^\d+月\d+日", "", text).rstrip("。.")
    body = re.sub(r"^(?:臺北市|台北市)", "", body)
    positive = r"(?:停止上班及上課|停止上班、停止上課|停止辦公上課|已達停止上班及上課標準)"
    negative = r"(?:照常上班及上課|未達停止上班及上課標準)"
    time_prefix = r"(?P<time>全天|上午|下午|晚上|晚間|(?:上午|下午|中午)\d+(?::\d+|時(?:\d+分)?)起)?"
    match = re.fullmatch(time_prefix + positive, body)
    if match:
        label = match.group("time") or "全天"
        period = {"全天": "full_day", "上午": "morning", "下午": "afternoon",
                  "晚上": "evening", "晚間": "evening"}.get(label, "partial")
        return dict(scope="citywide", period=period, work_suspended=True,
                    classes_suspended=True, review_required=False)
    if re.fullmatch(time_prefix + negative, body):
        return dict(scope="none", period="unknown", work_suspended=False,
                    classes_suspended=False, review_required=False)
    # A city-normal statement followed by named schools/villages must never
    # promote the entire city to a holiday. Keep the complete original wording.
    local_text = re.sub(r"\d+月\d+日(?:晚上|晚間)?" + negative + r"[。.;；]?", "", text)
    classes = bool(re.search(r"停止上課|停止上班及上課|停止辦公上課", local_text))
    work = bool(re.search(r"停止上班|停止辦公", local_text))
    school = bool(re.search(r"國小|國中|國民小學|高中|高工|中學|學校|幼兒園|藝校", local_text))
    village = bool(re.search(r"[\u4e00-\u9fff]{2,}里(?:、|及|停止)", local_text))
    if (classes or work) and (school or village) and "未達" not in local_text:
        partial = any(word in local_text for word in ("上午", "下午", "中午", "晚上", "晚間", "時起"))
        return dict(scope="partial_area" if village else "specific_schools",
                    period="partial" if partial else "full_day",
                    work_suspended=work, classes_suspended=classes, review_required=False)
    return dict(scope="review", period="unknown", work_suspended=None,
                classes_suspended=None, review_required=True)


def parse_official_csv(raw: bytes) -> dict:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != HEADERS:
            raise TaipeiClosureError("官方 CSV 欄位變動，停止更新並保留舊檔")
        records = []
        for number, row in enumerate(reader, 2):
            if set(row) != set(HEADERS) or any(not isinstance(value, str) or not value.strip() for value in row.values()):
                raise TaipeiClosureError(f"官方 CSV 第 {number} 筆缺少必要欄位")
            day = date(int(row["民國年"].strip()) + 1911, int(row["月"].strip()), int(row["日"].strip()))
            description = row[HEADERS[-1]].strip()
            records.append(dict(date=day.isoformat(), reason=row["天然災害名稱"].strip(),
                                description=description, source_url=DATASET_URL,
                                origin="official_dataset", **classify(description)))
    except (UnicodeError, csv.Error, TypeError, ValueError) as error:
        if isinstance(error, TaipeiClosureError):
            raise
        raise TaipeiClosureError(f"官方 CSV 無法解析：{error}") from error
    if not records:
        raise TaipeiClosureError("官方 CSV 為空，停止更新並保留舊檔")
    return dict(version=1, fetched_at=datetime.now(TAIPEI_TZ).isoformat(), source_url=DATASET_URL,
                download_url=DOWNLOAD_URL, source_sha256=hashlib.sha256(raw).hexdigest(),
                source_record_count=len(records), source_last_record_date=max(item["date"] for item in records),
                records=records)


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("records"), list):
            raise ValueError("必須是 version=1 且包含 records 陣列")
        return payload
    except (OSError, ValueError) as error:
        raise TaipeiClosureError(f"設定檔無效：{path}：{error}") from error


def record_key(record: dict) -> tuple:
    return record["date"], record["scope"], record["period"]


def validate_record(record: object, *, manual: bool = False) -> dict:
    if not isinstance(record, dict):
        raise TaipeiClosureError("records 的項目必須是物件")
    required = {"date", "reason", "scope", "period", "work_suspended", "classes_suspended"}
    allowed = required | {"description", "source_url", "enabled", "origin", "review_required"}
    if not required <= set(record) or set(record) - allowed:
        raise TaipeiClosureError("紀錄欄位有缺漏或未知欄位，請參考操作說明")
    iso_date(record["date"])
    if not isinstance(record["reason"], str) or not record["reason"].strip():
        raise TaipeiClosureError("reason 不可空白")
    if (not isinstance(record["scope"], str) or not isinstance(record["period"], str)
            or record["scope"] not in SCOPES or record["period"] not in PERIODS):
        raise TaipeiClosureError("scope 或 period 無效")
    if manual and (record["scope"] in {"none", "review"} or record["period"] == "unknown"):
        raise TaipeiClosureError("手動紀錄必須明確指定範圍與時段")
    for field in ("work_suspended", "classes_suspended"):
        if type(record[field]) is not bool and not (not manual and record["scope"] == "review" and record[field] is None):
            raise TaipeiClosureError(f"{field} 必須是 true 或 false")
    for field in ("description", "source_url"):
        if field in record and not isinstance(record[field], str):
            raise TaipeiClosureError(f"{field} 必須是字串")
    if "enabled" in record and type(record["enabled"]) is not bool:
        raise TaipeiClosureError("enabled 必須是 true 或 false")
    if manual and not (record["work_suspended"] or record["classes_suspended"]):
        raise TaipeiClosureError("手動紀錄至少需停班或停課其中之一")
    return dict(record)


def read_manual(path: Path = MANUAL_PATH) -> dict:
    payload = read_json(path)  # A missing/manual-corrupt file must not silently disappear.
    seen = set()
    for record in payload["records"]:
        checked = validate_record(record, manual=True)
        key = record_key(checked)
        if key in seen:
            raise TaipeiClosureError(f"手動紀錄重複：{key}，請修改原項目")
        seen.add(key)
    return payload


def merge_closures(snapshot: dict, manual: dict, as_of: date, years: int = 10) -> dict:
    first = window_start(as_of, years)
    merged = {}
    review = []
    for raw in snapshot["records"]:
        record = validate_record(raw)
        if record.get("origin") == "official_dataset":
            # Rebuild classifications from cached original text as well: parser
            # fixes must not require a second network download to take effect.
            record.update(classify(record.get("description", "")))
        if not first <= iso_date(record["date"]) <= as_of:
            continue
        if record["scope"] == "review":
            review.append(record)
        elif record["scope"] != "none":
            merged[record_key(record)] = record
    seen = set()
    for raw in manual["records"]:
        record = validate_record(raw, manual=True)
        key = record_key(record)
        if key in seen:
            raise TaipeiClosureError(f"手動紀錄重複：{key}")
        seen.add(key)
        if not first <= iso_date(record["date"]) <= as_of:
            continue
        if record.get("enabled", True):
            merged[key] = dict(record, origin="manual", review_required=False)
        else:
            merged.pop(key, None)
    records = sorted(merged.values(), key=record_key)
    full_dates = sorted({item["date"] for item in records if item["scope"] == "citywide"
                         and item["period"] == "full_day" and item["work_suspended"] and item["classes_suspended"]})
    return dict(version=1, generated_at=datetime.now(TAIPEI_TZ).isoformat(),
                range_start=first.isoformat(), range_end=as_of.isoformat(),
                source_url=DATASET_URL, source_fetched_at=snapshot["fetched_at"],
                source_record_count=snapshot["source_record_count"],
                source_last_record_date=snapshot["source_last_record_date"],
                coverage_note="官方歷史檔按年更新，最後紀錄日不代表完整覆蓋至今日；人工補登另附來源。未列日期不代表正常上班或股市有交易。",
                trading_calendar_applied=False, date_count=len({item["date"] for item in records}),
                citywide_full_day_dates=full_dates, records=records,
                review_required_records=review)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_json(path: Path, payload: dict) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def fetch_snapshot(*, timeout_seconds: float = 20) -> dict:
    request = Request(DOWNLOAD_URL, headers={"User-Agent": "CapitalAPI-TaipeiClosures/1.0", "Accept": "text/csv"})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(LIMIT_BYTES + 1)
    if len(raw) > LIMIT_BYTES:
        raise TaipeiClosureError("官方下載檔超過 2 MiB，停止更新")
    return parse_official_csv(raw)


def report_markdown(payload: dict) -> str:
    def escaped(value):
        return str(value).replace("|", "／").replace("\r", "").replace("\n", " ")

    lines = ["# 臺北市天然災害停班停課日期", "",
             f"查詢範圍：{payload['range_start']}～{payload['range_end']}（含起迄日）。",
             f"下載時間：{payload['source_fetched_at']}；官方原檔 {payload['source_record_count']} 筆，最後紀錄日 {payload['source_last_record_date']}。", "",
             f"共 {payload['date_count']} 個日期；其中全市全天同時停班停課 {len(payload['citywide_full_day_dates'])} 日。", "",
             "注意：這是天然災害公告，不是所有國定假日、疫情停課紀錄，也不是股市休市日曆。官方歷史檔按年更新，近期缺漏須另查公告；未列不代表有交易。",
             "本次未套用正式回補或修改資料庫／API。人工新增來源與官方下載分開保存，重新下載不覆蓋人工檔。", "",
             "來源：[臺北市資料大平臺](" + DATASET_URL + ")；最新補登見各筆來源。",
             "操作方式：[新增、停用及重新下載](TAIPEI_CLOSURES.md)。", "",
             "## 全市全天同時停班停課", "",
             "| 日期 | 原因 | 來源 |", "| --- | --- | --- |"]
    for record in payload["records"]:
        if record["date"] in payload["citywide_full_day_dates"] and record["scope"] == "citywide" and record["period"] == "full_day":
            source = record.get("source_url", "")
            label = "官方歷史檔" if record.get("origin") == "official_dataset" else "人工補登（附來源）"
            attribution = f"[{label}]({source})" if source else "人工補登（未提供來源）"
            lines.append(f"| {record['date']} | {escaped(record['reason'])} | {attribution} |")
    lines += ["", "## 局部地區、指定學校或部分時段（不可視為全市全天）", "",
              "| 日期 | 原因 | 範圍 | 時段 | 停班 | 停課 | 原公告 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for record in payload["records"]:
        if record["scope"] == "citywide" and record["period"] == "full_day" and record["work_suspended"] and record["classes_suspended"]:
            continue
        lines.append("| " + " | ".join(escaped(value) for value in (
            record["date"], record["reason"], SCOPES[record["scope"]], PERIODS[record["period"]],
            "是" if record["work_suspended"] else "否", "是" if record["classes_suspended"] else "否",
            record.get("description", ""))) + " |")
    lines += ["", "## 待人工確認", "",
              "下列文字無法可靠分類，保留原文，不納入任何自動排除日期。", ""]
    lines += [f"- {item['date']}：{escaped(item['description'])}" for item in payload["review_required_records"]] or ["本範圍沒有未分類紀錄。"]
    return "\n".join(lines) + "\n"


def build_files(snapshot: dict, manual: dict, as_of: date, *, years: int = 10,
                merged_path: Path = MERGED_PATH, report_path: Path = REPORT_PATH) -> dict:
    merged = merge_closures(snapshot, manual, as_of, years)
    report = report_markdown(merged)  # Validate/render before replacing any output.
    save_json(merged_path, merged)
    atomic_text(report_path, report)
    return merged


def refresh_files(as_of: date, *, years: int = 10, snapshot_path: Path = SNAPSHOT_PATH,
                  manual_path: Path = MANUAL_PATH, merged_path: Path = MERGED_PATH,
                  report_path: Path = REPORT_PATH) -> dict:
    manual = read_manual(manual_path)  # Bad user edits must never be ignored.
    snapshot = fetch_snapshot()
    if snapshot_path.exists():
        previous = read_json(snapshot_path)
        if (snapshot["source_record_count"] < previous["source_record_count"]
                or snapshot["source_last_record_date"] < previous["source_last_record_date"]):
            raise TaipeiClosureError("官方筆數或最新紀錄倒退，停止更新並保留舊檔，請人工確認")
    merged = merge_closures(snapshot, manual, as_of, years)
    report = report_markdown(merged)
    save_json(snapshot_path, snapshot)
    save_json(merged_path, merged)
    atomic_text(report_path, report)
    return merged
