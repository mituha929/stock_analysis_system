import contextlib
import csv
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from app.services import taipei_closures as closures
from scripts import manage_taipei_closures as cli


def csv_bytes(*rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(closures.HEADERS)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def snapshot():
    return closures.parse_official_csv(csv_bytes(
        (105, 10, 23, "排除範圍外", "10月23日停止上班及上課。"),
        (105, 10, 24, "含起日", "10月24日停止上班及上課。"),
        (113, 7, 24, "凱米颱風", "\t7月24日停止上班及上課。"),
        (114, 10, 23, "1020豪雨", "10月23日照常上班及上課。臺北市士林區陽明山國民小學停止上班、停止上課。"),
        (115, 7, 12, "巴威颱風", "7月12日照常上班及上課。"),
    ))


def manual_record(**changes):
    record = dict(date="2026-10-23", reason="薔琵颱風", scope="citywide", period="full_day",
                  work_suspended=True, classes_suspended=True, enabled=True)
    record.update(changes)
    return record


class TaipeiClosureParsingTests(unittest.TestCase):
    def test_roc_date_bom_multiline_and_whitespace(self):
        parsed = closures.parse_official_csv(csv_bytes(
            ("113 ", "4", "3", "地震", "4月3日照常上班及上課。\n民族國中全天照常上班、停止上課。"),
        ))
        record = parsed["records"][0]
        self.assertEqual(record["date"], "2024-04-03")
        self.assertIn("\n", record["description"])
        self.assertEqual(record["scope"], "specific_schools")
        self.assertFalse(record["work_suspended"])
        self.assertTrue(record["classes_suspended"])

    def test_negative_words_are_not_closures(self):
        for text in ("9月3日未達停止上班及上課標準。", "8月8日晚上照常上班及上課。"):
            with self.subTest(text=text):
                self.assertEqual(closures.classify(text)["scope"], "none")

    def test_citywide_full_day_and_legacy_wording(self):
        for text in ("\t7月24日停止上班及上課。", "7月30日已達停止上班及上課標準。", "10月16日停止辦公上課。"):
            with self.subTest(text=text):
                parsed = closures.classify(text)
                self.assertEqual((parsed["scope"], parsed["period"]), ("citywide", "full_day"))

    def test_evening_and_from_four_pm_are_not_full_day(self):
        self.assertEqual(closures.classify("7月29日晚上已達停止上班及上課標準。")["period"], "evening")
        self.assertEqual(closures.classify("7月10日下午4時起停止上班及上課。")["period"], "partial")

    def test_local_villages_and_schools_are_not_citywide(self):
        for text in (
            "10月25日照常上班及上課。臺北市北投區湖田里、陽明山國民小學停止上班、停止上課。",
            "11月11日照常上班及上課。臺北市士林區永福里、新安里停止上班、停止上課。",
        ):
            with self.subTest(text=text):
                self.assertEqual(closures.classify(text)["scope"], "partial_area")

    def test_primary_school_full_name_is_local_not_unknown(self):
        parsed = closures.classify("10月23日照常上班及上課。臺北市士林區陽明山國民小學停止上班、停止上課。")
        self.assertEqual(parsed["scope"], "specific_schools")
        self.assertEqual(parsed["period"], "full_day")
        self.assertTrue(parsed["work_suspended"])

    def test_school_only_partial_hours(self):
        parsed = closures.classify("6月2日清江國小附設幼兒園中午12時起照常上班、停止上課。格致國中下午2時起照常上班、停止上課。")
        self.assertEqual((parsed["scope"], parsed["period"]), ("specific_schools", "partial"))
        self.assertFalse(parsed["work_suspended"])

    def test_unknown_wording_is_retained_for_review(self):
        parsed = closures.classify("依各機關另行通知。")
        self.assertEqual(parsed["scope"], "review")
        self.assertIsNone(parsed["work_suspended"])

    def test_header_changes_empty_csv_and_invalid_dates_are_rejected(self):
        for raw in (b"date,name\n2026-10-23,typhoon", csv_bytes(),
                    csv_bytes((114, 2, 29, "地震", "2月29日停止上班及上課。")),
                    csv_bytes((115, 7, 10, "", "7月10日停止上班及上課。"))):
            with self.subTest(raw=raw):
                with self.assertRaises(closures.TaipeiClosureError):
                    closures.parse_official_csv(raw)

    def test_strict_iso_dates_and_leap_year_window(self):
        self.assertEqual(closures.window_start(date(2024, 2, 29)), date(2014, 2, 28))
        for invalid in ("20261023", "2026-W43-5", "2026-02-29", 20261023):
            with self.assertRaises(closures.TaipeiClosureError):
                closures.iso_date(invalid)


class TaipeiClosureMergeTests(unittest.TestCase):
    def test_rolling_ten_year_bounds_and_different_year_same_day(self):
        merged = closures.merge_closures(snapshot(), {"version": 1, "records": [manual_record()]}, date(2026, 10, 24))
        self.assertEqual(merged["range_start"], "2016-10-24")
        self.assertIn("2016-10-24", merged["citywide_full_day_dates"])
        self.assertNotIn("2016-10-23", merged["citywide_full_day_dates"])
        self.assertNotIn("2025-10-23", merged["citywide_full_day_dates"])
        self.assertIn("2026-10-23", merged["citywide_full_day_dates"])
        self.assertFalse(merged["trading_calendar_applied"])

    def test_manual_same_key_overrides_and_disabled_suppresses_not_snapshot(self):
        original = snapshot()
        before = json.dumps(original)
        for enabled in (True, False):
            merged = closures.merge_closures(original, {"records": [manual_record(date="2024-07-24", reason="補充說明", enabled=enabled)]}, date(2026, 10, 24))
            rows = [item for item in merged["records"] if item["date"] == "2024-07-24"]
            self.assertEqual(len(rows), int(enabled))
            if enabled:
                self.assertEqual((rows[0]["reason"], rows[0]["origin"]), ("補充說明", "manual"))
        self.assertEqual(json.dumps(original), before)

    def test_deduplicates_official_same_key_not_other_scopes(self):
        official = snapshot()
        official["records"] += [official["records"][2]]
        local = manual_record(date="2024-07-24", scope="specific_schools")
        merged = closures.merge_closures(official, {"records": [local]}, date(2026, 10, 24))
        self.assertEqual(len([item for item in merged["records"] if item["date"] == "2024-07-24"]), 2)

    def test_unknown_does_not_enter_full_day_dates(self):
        official = closures.parse_official_csv(csv_bytes((115, 10, 20, "待確認", "暫停活動，其他另行公告。")))
        merged = closures.merge_closures(official, {"records": []}, date(2026, 10, 24))
        self.assertFalse(merged["records"])
        self.assertEqual(len(merged["review_required_records"]), 1)

    def test_build_reclassifies_old_snapshot_text_without_network(self):
        official = snapshot()
        official["records"][3].update(scope="review", period="unknown", work_suspended=None,
                                      classes_suspended=None, review_required=True)
        with patch.object(closures, "urlopen", side_effect=AssertionError("no network")):
            merged = closures.merge_closures(official, {"records": []}, date(2026, 10, 24))
        record = next(item for item in merged["records"] if item["date"] == "2025-10-23")
        self.assertEqual(record["scope"], "specific_schools")
        self.assertNotIn("2025-10-23", merged["citywide_full_day_dates"])

    def test_invalid_manual_is_not_silently_ignored(self):
        bad_records = [manual_record(date="2026-02-30"), manual_record(enabled="false"),
                       manual_record(work_suspended="true"), manual_record(scope="all"),
                       manual_record(unknown_field=True), manual_record(reason=" ")]
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(closures.TaipeiClosureError):
                    closures.merge_closures(snapshot(), {"records": [record]}, date(2026, 10, 24))
        with self.assertRaises(closures.TaipeiClosureError):
            closures.merge_closures(snapshot(), {"records": [manual_record(), manual_record()]}, date(2026, 10, 24))

    def test_report_does_not_attribute_unverified_manual_entry_to_official_source(self):
        merged = closures.merge_closures(snapshot(), {"records": [manual_record(reason="個人新增")]}, date(2026, 10, 24))
        self.assertIn("| 2026-10-23 | 個人新增 | 人工補登（未提供來源） |", closures.report_markdown(merged))


class TaipeiClosurePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.paths = dict(snapshot_path=root / "official.json", manual_path=root / "manual.json",
                          merged_path=root / "merged.json", report_path=root / "report.md")
        closures.save_json(self.paths["snapshot_path"], snapshot())
        closures.save_json(self.paths["manual_path"], dict(version=1, records=[manual_record(
            description="原始查證摘要", source_url="https://www.gov.taipei/")]))

    def test_refresh_preserves_manual_and_is_repeatable(self):
        manual_before = self.paths["manual_path"].read_bytes()
        with patch.object(closures, "fetch_snapshot", return_value=snapshot()):
            first = closures.refresh_files(date(2026, 10, 24), **self.paths)
            second = closures.refresh_files(date(2026, 10, 24), **self.paths)
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(self.paths["manual_path"].read_bytes(), manual_before)
        self.assertTrue(self.paths["report_path"].exists())

    def test_fetch_failure_and_truncated_source_keep_existing_outputs(self):
        closures.build_files(snapshot(), {"records": [manual_record()]}, date(2026, 10, 24),
                             merged_path=self.paths["merged_path"], report_path=self.paths["report_path"])
        before = {key: path.read_bytes() for key, path in self.paths.items()}
        smaller = closures.parse_official_csv(csv_bytes((115, 7, 12, "測試", "7月12日照常上班及上課。")))
        for failure in (OSError("offline"), None):
            with patch.object(closures, "fetch_snapshot", side_effect=failure, return_value=smaller):
                with self.assertRaises((OSError, closures.TaipeiClosureError)):
                    closures.refresh_files(date(2026, 10, 24), **self.paths)
            self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, before)

    def test_invalid_manual_file_is_not_overwritten(self):
        self.paths["manual_path"].write_text('{"version":1,"records":[{"date":"bad"}]}', encoding="utf-8")
        before = self.paths["manual_path"].read_bytes()
        with patch.object(closures, "fetch_snapshot") as fetch:
            with self.assertRaises(closures.TaipeiClosureError):
                closures.refresh_files(date(2026, 10, 24), **self.paths)
        fetch.assert_not_called()
        self.assertEqual(self.paths["manual_path"].read_bytes(), before)

    def test_network_response_is_bounded_and_validated(self):
        response = MagicMock()
        response.read.return_value = csv_bytes((115, 7, 10, "巴威颱風", "7月10日停止上班及上課。"))
        with patch.object(closures, "urlopen") as opener:
            opener.return_value.__enter__.return_value = response
            self.assertEqual(closures.fetch_snapshot()["source_record_count"], 1)
            response.read.assert_called_once_with(closures.LIMIT_BYTES + 1)
            self.assertTrue(opener.call_args.args[0].full_url.startswith("https://data.taipei/"))

    def test_cli_add_is_idempotent_and_list_is_read_only(self):
        with contextlib.ExitStack() as stack:
            for name, path in zip(("SNAPSHOT_PATH", "MANUAL_PATH", "MERGED_PATH", "REPORT_PATH"), self.paths.values()):
                stack.enter_context(patch.object(cli, name, path))
            stack.enter_context(patch.object(cli, "taipei_today", return_value=date(2026, 10, 24)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for _ in range(2):
                self.assertEqual(cli.main(["add", "2026-10-23", "--reason", "補充說明", "--as-of", "2026-10-24"]), 0)
            self.assertEqual(len(closures.read_manual(self.paths["manual_path"])["records"]), 1)
            saved = closures.read_manual(self.paths["manual_path"])["records"][0]
            self.assertEqual(saved["description"], "原始查證摘要")
            self.assertEqual(saved["source_url"], "https://www.gov.taipei/")
            before = {key: path.read_bytes() for key, path in self.paths.items()}
            self.assertEqual(cli.main(["list", "--as-of", "2026-10-24"]), 0)
            self.assertEqual({key: path.read_bytes() for key, path in self.paths.items()}, before)


if __name__ == "__main__":
    unittest.main()
