from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
import json
import unittest
from unittest.mock import MagicMock, patch

from app.services.external_market_quotes import (
    CMONEY_PAGE, CM_FIELDS, INDEX_NAMES, QuoteFetchError, fetch_all, number,
    parse_cmoney, parse_cnyes, read_public_text, validate_batch,
)
from app.services.external_market_quote_repository import (
    COLUMNS, TABLE, ExternalMarketQuoteRepository, QuoteWriteError, quote_row,
)
from app.services.mysql_instrument_repository import MySQLSettings
from scripts.fetch_external_market_quotes import run


NOW = datetime(2026, 10, 22, 3, 30, tzinfo=timezone.utc)
CM_EXAMPLE = {"成交": "47482.00", "漲跌": "469.00", "漲跌幅": "1.00", "開盤": "46991.00",
              "最高": "47555.00", "最低": "46983.00", "金額(億)": "16.89", "總量": "35705.00"}


def index_payload(symbol="DJI", **changes):
    row = {"0": f"GI:{symbol}:INDEX", "200009": INDEX_NAMES[symbol], "200007": int(NOW.timestamp()) - 60,
           "6": "100.125", "11": "1.125", "56": "1.14", "19": "99.125", "12": "101.125", "13": "98.125"}
    row.update(changes)
    return json.dumps({"statusCode": 200, "data": [row]})


def cm_html(*, values=None, url=CMONEY_PAGE, ticker="TXF1", omit=None, duplicate=None, visible_changes=None):
    values = values or CM_EXAMPLE
    props = [{"name": label, "value": value} for label, value in values.items() if label != omit]
    if duplicate:
        props.append({"name": duplicate, "value": values[duplicate]})
    graph = [{"@type": "WebPage", "url": url, "name": "台指期夜盤", "dateModified": "2026-10-21T16:00:00.000Z"},
             {"@id": url + "#entity", "tickerSymbol": ticker, "additionalProperty": props}]
    text = '<script type="application/ld+json">' + json.dumps({"@graph": graph}) + '</script>'
    for label, val in values.items():
        if label == omit:
            continue
        value = (visible_changes or {}).get(label, val + "%" if label == "漲跌幅" else val)
        text += f'<div class="row"><span class="row__key">{label}</span><span class="row__value">{value}</span></div>'
    return text


def batch():
    return [parse_cnyes(index_payload(symbol), symbol, NOW) for symbol in INDEX_NAMES] + [parse_cmoney(cm_html(), NOW)]


class ExternalMarketParserTests(unittest.TestCase):
    def test_decimal_percent_and_comma_handling(self):
        self.assertEqual(number("−1,234.50", "field"), Decimal("-1234.50"))
        self.assertEqual(number("+1.00%", "field", percent=True), Decimal("1.00"))

    def test_invalid_numeric_never_becomes_zero(self):
        for value in (None, True, "--", "", "NaN", "Infinity", "1,23", "1e3", "10000000000000000", "0.123456789"):
            with self.subTest(value=value), self.assertRaises(QuoteFetchError):
                number(value, "field")

    def test_index_preserves_exact_precision_and_timestamp(self):
        quote = parse_cnyes(index_payload(), "DJI", NOW)
        self.assertEqual(quote.last_price, Decimal("100.125"))
        self.assertEqual(quote.quote_at, NOW - timedelta(seconds=60))
        self.assertEqual(quote.to_dict()["quote_time_status"], "source_timestamp")

    def test_index_json_number_not_float(self):
        payload = index_payload().replace('"100.125"', '100.125')
        self.assertEqual(parse_cnyes(payload, "DJI", NOW).last_price, Decimal("100.125"))

    def test_wrong_index_symbol_rejected(self):
        with self.assertRaises(QuoteFetchError):
            parse_cnyes(index_payload("INX"), "DJI", NOW)

    def test_bad_index_response_or_timestamp_rejected(self):
        for payload in ('{}', '{', '{"statusCode":403,"data":[]}', index_payload(**{"200007": True}), index_payload(**{"200007": "1792613461"})):
            with self.subTest(payload=payload), self.assertRaises(QuoteFetchError):
                parse_cnyes(payload, "DJI", NOW)

    def test_missing_index_field_rejected(self):
        data = json.loads(index_payload())
        del data["data"][0]["19"]
        with self.assertRaises(QuoteFetchError):
            parse_cnyes(json.dumps(data), "DJI", NOW)

    def test_cmoney_eight_summary_fields_and_no_fabricated_time(self):
        quote = parse_cmoney(cm_html(), NOW)
        self.assertEqual(quote.last_price, Decimal("47482"))
        self.assertEqual(quote.amount_yi, Decimal("16.89"))
        self.assertEqual(quote.total_volume, 35705)
        self.assertEqual(quote.change_percent, Decimal("1.00"))
        self.assertIsNone(quote.quote_at)
        self.assertEqual(quote.to_dict()["quote_time_status"], "not_provided")

    def test_chart_tooltip_cannot_override_summary(self):
        html = cm_html() + '<div class="tooltip">最高 47485 最低 47477</div>'
        quote = parse_cmoney(html, NOW)
        self.assertEqual((quote.high_price, quote.low_price), (Decimal("47555"), Decimal("46983")))

    def test_day_session_or_wrong_ticker_rejected(self):
        for html in (cm_html(url=CMONEY_PAGE.split("?")[0]), cm_html(ticker="MTX1")):
            with self.assertRaises(QuoteFetchError):
                parse_cmoney(html, NOW)

    def test_all_required_cmoney_fields_must_exist(self):
        for label in CM_FIELDS.values():
            with self.subTest(label=label), self.assertRaises(QuoteFetchError):
                parse_cmoney(cm_html(omit=label), NOW)

    def test_duplicate_or_conflicting_summary_rejected(self):
        for html in (cm_html(duplicate="最高"), cm_html(visible_changes={"最高": "47485"}), cm_html() + '<span class="row__key">成交</span><span class="row__value">47482</span>'):
            with self.assertRaises(QuoteFetchError):
                parse_cmoney(html, NOW)

    def test_bad_volume_rejected_without_correction(self):
        for value in ("-1", "3.5", "18446744073709551616"):
            with self.subTest(value=value), self.assertRaises(QuoteFetchError):
                parse_cmoney(cm_html(values=CM_EXAMPLE | {"總量": value}), NOW)

    def test_unsigned_bigint_boundary_volume_is_exact(self):
        quote = parse_cmoney(cm_html(values=CM_EXAMPLE | {"總量": "18,446,744,073,709,551,615.00"}), NOW)
        self.assertEqual(quote.total_volume, 18446744073709551615)

    def test_high_low_and_future_time_validation(self):
        for payload in (index_payload(**{"12": "99"}), index_payload(**{"13": "0"}), index_payload(**{"200007": int((NOW + timedelta(hours=1)).timestamp())})):
            with self.assertRaises(QuoteFetchError):
                parse_cnyes(payload, "DJI", NOW)

    def test_closed_market_old_timestamp_not_silently_changed(self):
        epoch = int((NOW - timedelta(days=4)).timestamp())
        quote = parse_cnyes(index_payload(**{"200007": epoch}), "DJI", NOW)
        self.assertEqual(quote.quote_at, NOW - timedelta(days=4))

    def test_timezone_required(self):
        q = batch()[0]
        with self.assertRaises(QuoteFetchError):
            replace(q, fetched_at=NOW.replace(tzinfo=None)).validate()

    def test_missing_or_duplicate_batch_rejected(self):
        quotes = batch()
        for items in (quotes[:4], quotes + [quotes[0]], quotes[:4] + [quotes[0]]):
            with self.assertRaises(QuoteFetchError):
                validate_batch(items)

    def test_fetch_all_is_live_reader_not_embedded_examples(self):
        def reader(url):
            if url == CMONEY_PAGE:
                return cm_html(values=CM_EXAMPLE | {"金額(億)": "17.89"})
            symbol = url.split("GI:")[1].split(":")[0]
            return index_payload(symbol, **{"6": "100.875", "200007": int(datetime.now(timezone.utc).timestamp()) - 60})
        quotes = fetch_all(reader=reader)
        self.assertEqual(quotes[0].last_price, Decimal("100.875"))
        self.assertEqual(quotes[-1].amount_yi, Decimal("17.89"))

    def test_http_403_not_retried_or_replaced(self):
        from urllib.error import HTTPError
        with patch("app.services.external_market_quotes.urlopen", side_effect=HTTPError(CMONEY_PAGE, 403, "Forbidden", {}, None)) as opened:
            with self.assertRaisesRegex(QuoteFetchError, "HTTP 403"):
                read_public_text(CMONEY_PAGE)
        opened.assert_called_once()

    def test_http_oversize_and_redirect_rejected(self):
        for target, content in ((CMONEY_PAGE + "other", b"{}"), (CMONEY_PAGE, b"x" * 2_000_001)):
            response = MagicMock()
            response.geturl.return_value, response.read.return_value = target, content
            with patch("app.services.external_market_quotes.urlopen") as opened:
                opened.return_value.__enter__.return_value = response
                with self.assertRaises(QuoteFetchError):
                    read_public_text(CMONEY_PAGE)


class ExternalMarketRepositoryTests(unittest.TestCase):
    def repository(self):
        repo = ExternalMarketQuoteRepository(MySQLSettings("fake", 3306, "fake", "unit-only", "unit"))
        connection, cursor = MagicMock(), MagicMock()
        connection.cursor.return_value = cursor
        repo._connector, repo._connect_database = MagicMock(), MagicMock(return_value=connection)
        return repo, connection, cursor

    def test_invalid_batch_never_connects_or_creates_table(self):
        repo, con, cur = self.repository()
        with self.assertRaises(QuoteFetchError):
            repo.save_batch(batch()[:4])
        repo._connect_database.assert_not_called()
        cur.execute.assert_not_called()

    def test_atomic_five_rows_exact_readback_and_only_new_table(self):
        repo, con, cur = self.repository()
        quotes = batch()
        cur.fetchall.return_value = sorted([quote_row(q) for q in quotes], key=lambda x: (x[0], x[1], x[3]))
        batch_id = repo.save_batch(quotes)
        self.assertRegex(batch_id, r"^[0-9a-f]{32}$")
        self.assertEqual(con.commit.call_count, 2)
        con.rollback.assert_not_called()
        con.start_transaction.assert_called_once_with()
        sql, rows = cur.executemany.call_args.args
        self.assertIn(TABLE, sql)
        self.assertNotIn("candles", sql)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row[0] == batch_id for row in rows))
        self.assertTrue(all("candles" not in call.args[0] for call in cur.execute.call_args_list))

    def test_insert_failure_rolls_back_and_no_final_commit(self):
        repo, con, cur = self.repository()
        cur.executemany.side_effect = RuntimeError("sensitive connector details")
        with self.assertRaises(QuoteWriteError) as ctx:
            repo.save_batch(batch())
        self.assertFalse(ctx.exception.commit_uncertain)
        self.assertNotIn("sensitive", str(ctx.exception))
        self.assertEqual(con.commit.call_count, 1)
        con.rollback.assert_called_once()

    def test_readback_mismatch_rolls_back(self):
        repo, con, cur = self.repository()
        cur.fetchall.return_value = []
        with self.assertRaises(QuoteWriteError):
            repo.save_batch(batch())
        self.assertEqual(con.commit.call_count, 1)
        con.rollback.assert_called_once()

    def test_commit_failure_marks_outcome_uncertain(self):
        repo, con, cur = self.repository()
        cur.fetchall.return_value = sorted([quote_row(q) for q in batch()], key=lambda x: (x[0], x[1], x[3]))
        con.commit.side_effect = [None, RuntimeError("lost connection")]
        with self.assertRaises(QuoteWriteError) as ctx:
            repo.save_batch(batch())
        self.assertTrue(ctx.exception.commit_uncertain)
        self.assertRegex(ctx.exception.batch_id, r"^[0-9a-f]{32}$")

    def test_cli_dry_run_never_loads_credentials_or_db(self):
        with patch("scripts.fetch_external_market_quotes.fetch_all", return_value=batch()), patch("dotenv.load_dotenv") as env, patch("scripts.fetch_external_market_quotes.ExternalMarketQuoteRepository") as repo, patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(run(write=False), 0)
        env.assert_not_called()
        repo.assert_not_called()

    def test_source_failure_never_writes_even_with_write_flag(self):
        with patch("scripts.fetch_external_market_quotes.fetch_all", side_effect=QuoteFetchError("bad source")), patch("scripts.fetch_external_market_quotes.ExternalMarketQuoteRepository") as repo:
            with self.assertRaises(QuoteFetchError):
                run(write=True)
        repo.assert_not_called()

    def test_cli_postcommit_readback_confirms_all_fields(self):
        quotes = batch()
        rows = [dict(zip(COLUMNS, row)) for row in sorted([quote_row(q) for q in quotes], key=lambda x: (x[0], x[1], x[3]))]
        with patch("scripts.fetch_external_market_quotes.fetch_all", return_value=quotes), patch("dotenv.load_dotenv"), patch("scripts.fetch_external_market_quotes.ExternalMarketQuoteRepository") as factory, patch("sys.stdout", new_callable=StringIO):
            repo = factory.return_value
            repo.save_batch.return_value = "a" * 32
            repo.read_batch.return_value = rows
            self.assertEqual(run(write=True), 0)
            rows[0]["last_price"] = Decimal("1")
            self.assertEqual(run(write=True), 2)

    def observation(self, *, previous=None, checked=None, previous_scope=None):
        repo, con, cur = self.repository()
        quote = batch()[0]
        actual = dict(zip(COLUMNS, quote_row(quote)))
        rows = [{"acquired": 1}, checked, previous]
        if previous_scope is not None:
            rows.append({"quality_json": json.dumps({"market_session_key": previous_scope})})
        if previous is None:
            rows.append(actual)
        cur.fetchone.side_effect = rows
        cur.lastrowid = 77
        return repo, con, cur, quote

    def test_individual_observation_is_atomic_and_exact(self):
        repo, con, cur, quote = self.observation()
        quality = {"market_session_key": "NYSE:2026-10-22", "close_pending": False}
        result = repo.save_observation(quote, "test-slot", quality)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["snapshot_id"], 77)
        self.assertEqual(con.commit.call_count, 2)
        self.assertTrue(any("external_market_quote_checks" in call.args[0] for call in cur.execute.call_args_list))

    def test_identical_source_timestamp_and_values_only_record_check(self):
        quote = batch()[0]
        previous = dict(zip(COLUMNS, quote_row(quote))) | {"id": 1, "batch_id": "a"*32}
        repo, con, cur, quote = self.observation(previous=previous)
        result = repo.save_observation(quote, "next-slot", {"market_session_key": "NYSE:2026-10-22"})
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(any(call.args[0].startswith("INSERT INTO `external_market_quote_snapshots`") for call in cur.execute.call_args_list))

    def test_database_slot_check_prevents_duplicate_after_local_state_loss(self):
        checked = dict(status="updated", snapshot_id=1, batch_id="a"*32, quality_json='{"close_pending":false}',
                       checked_at_utc=NOW.replace(tzinfo=None), quote_at_utc=(NOW-timedelta(seconds=60)).replace(tzinfo=None))
        repo, con, cur, quote = self.observation(checked=checked)
        result = repo.save_observation(quote,"same-slot",{"market_session_key":"NYSE:2026-10-22"})
        self.assertEqual(result["status"],"already_checked")
        con.rollback.assert_called_once()

    def test_invalid_individual_quote_never_connects(self):
        repo, con, cur = self.repository()
        with self.assertRaises(QuoteFetchError):
            repo.save_observation(replace(batch()[0],low_price=Decimal("999999")),"slot",{"market_session_key":"NYSE:2026-10-22"})
        repo._connect_database.assert_not_called()

    def test_individual_commit_failure_remains_uncertain_if_cleanup_fails(self):
        repo, con, cur, quote = self.observation()
        con.commit.side_effect = [None, RuntimeError("lost connection")]
        con.rollback.side_effect = RuntimeError("rollback unavailable")
        with self.assertRaises(QuoteWriteError) as caught:
            repo.save_observation(quote,"slot",{"market_session_key":"NYSE:2026-10-22"})
        self.assertTrue(caught.exception.commit_uncertain)


if __name__ == "__main__":
    unittest.main()
