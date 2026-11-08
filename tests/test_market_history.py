"""Offline checks for bounded history collection and source period validation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import unittest
from unittest.mock import MagicMock, Mock

from scripts.collect_market_history import (
    Job, Reader, SECURITY_CODE, daily_jobs, inspect, month_periods, number, official_day,
    quarter_periods, save_json,
)
from scripts.audit_market_history import audit


class MarketHistoryTests(unittest.TestCase):
    def test_exact_60_months_and_20_quarters_across_years(self):
        months=month_periods('2026-09')
        self.assertEqual((len(months),months[0],months[-1]),(60,'2021-10','2026-09'))
        quarters=quarter_periods('2026-Q3')
        self.assertEqual((len(quarters),quarters[0],quarters[-1]),(20,'2021-Q4','2026-Q3'))
        self.assertEqual(len(set(quarters)),20)

    def test_tpex_date_uses_official_form_format(self):
        for job in daily_jobs('2026-09-07'):
            if job.market=='TPEx':self.assertEqual(job.parameters['date'],'2026/09/07')

    def test_tpex_market_scopes_have_distinct_keys_and_queries(self):
        jobs=[j for j in daily_jobs('2026-09-07') if j.market=='TPEx' and j.dataset=='institutional_market']
        self.assertEqual({j.parameters['prod'] for j in jobs},{'0','1'})
        self.assertEqual(len({j.key for j in jobs}),2)

    def test_security_codes_preserve_etf_tdr_and_leading_zeros(self):
        for code in ('2330','0050','00411A','910322'):
            self.assertIsNotNone(SECURITY_CODE.fullmatch(code))
        for code in ('合計','233','2330 ', '1234567'):
            self.assertIsNone(SECURITY_CODE.fullmatch(code))

    def test_ignored_historical_date_cannot_be_accepted(self):
        job=Job('institutional_market','TPEx','2026-09-07','https://www.tpex.org.tw/example')
        raw=b'{"stat":"ok","date":"20261104","tables":[{"fields":["Name","Buy","Sell","Net"],"data":[["Foreign","1","2","-1"]]}]}'
        result=inspect(job,{'http_status':200},raw)
        self.assertEqual(result['status'],'pending')
        self.assertIn('response_date_mismatch',result['reason'])
        self.assertNotIn('records',result)

    def test_offline_replay_never_opens_network(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            reader=Reader(folder,offline=True)
            reader.opener=Mock()
            job=Job('income','TWSE','2026-Q3','https://mopsov.twse.com.tw/example')
            meta,raw=reader.read(job)
            reader.opener.open.assert_not_called()
            self.assertEqual(meta['error'],'not_in_cache')
            self.assertEqual(raw,b'')

    def test_explicit_transient_retry_retains_history_and_never_retries_http_block(self):
        job=Job('margin','TWSE','2026-09-07','https://www.twse.com.tw/example')
        cache=hashlib.sha256(job.url.encode()+b'\n').hexdigest()
        for status,error,old_raw,expected_calls in ((None,'URLError',b'',1),(502,'HTTP 502',b'bad gateway',1),(403,'HTTP 403',b'blocked',0)):
            with self.subTest(status=status),TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
                reader=Reader(temporary,retry_transient_errors=True)
                folder=Path(temporary)
                save_json(folder/'raw'/f'{cache}.json',{'http_status':status,'error':error})
                (folder/'raw'/f'{cache}.bin').write_bytes(old_raw)
                response=MagicMock()
                response.__enter__.return_value=response
                response.code=200;response.headers={'Content-Type':'application/json'}
                response.read.return_value=b'{"stat":"OK"}'
                reader.opener=Mock()
                reader.opener.open.return_value=response
                result,raw=reader.read(job)
                self.assertEqual(reader.opener.open.call_count,expected_calls)
                if expected_calls:
                    self.assertEqual(result['attempt_history'][0]['error'],error)
                    self.assertEqual((folder/result['attempt_history'][0]['body_file']).read_bytes(),old_raw)
                    self.assertEqual(result['http_status'],200)
                    reader.read(job)
                    reader.opener.open.assert_called_once()
                else:self.assertEqual(raw,b'blocked')

    def test_precision_missing_and_negative_values_are_distinct(self):
        self.assertEqual(number('18,446,744,073,709,551,615'),'18446744073709551615')
        self.assertEqual(number('−1,234.50'),'-1234.50')
        self.assertEqual(number('0'),'0')
        self.assertIsNone(number('--'))
        for bad in ('NaN','Infinity','1,23','1e6'):
            with self.assertRaises(ValueError):number(bad)

    def test_calendar_date_validation(self):
        self.assertEqual(official_day('115/09/07'),'2026-09-07')
        self.assertEqual(official_day('20260907'),'2026-09-07')
        with self.assertRaises(ValueError):official_day('20260230')

    def test_income_is_ytd_and_eps_has_its_own_unit(self):
        job=Job('income','TWSE','2026-Q3','https://mopsov.twse.com.tw/example')
        html='<h2>上市公司第三季資料</h2><p>本表各季別資料係揭露該年度截至該季止之累計金額。</p><table><tr><th>公司代號</th><th>公司名稱</th><th>營業收入</th><th>營業毛利（毛損）</th><th>基本每股盈餘（元）</th></tr><tr><td>1215</td><td>卜蜂</td><td>21,866,426</td><td>--</td><td>5.47</td></tr></table>'
        result=inspect(job,{'http_status':200},html.encode())
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['value_basis'],'year_to_date')
        self.assertEqual(result['units']['基本每股盈餘（元）'],'TWD/share')
        self.assertEqual(result['units']['營業收入'],'TWD_thousand')
        self.assertIsNone(result['records'][0]['values']['營業毛利（毛損）'])
        wrong=inspect(Job('income','TWSE','2026-Q2',job.url),{'http_status':200},html.encode())
        self.assertEqual(wrong['status'],'pending')

    def test_audit_does_not_accept_unprocessed_periods(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            folder=Path(temporary)
            save_json(folder/'jobs.json',[{'dataset':'margin','market':'TWSE','period':'2026-09-07','segment':''}])
            result=audit(folder)
            self.assertFalse(result['complete'])
            self.assertEqual(result['issues'][0]['reason'],'not_processed')

    def test_audit_flags_inconsistent_market_net_and_tampered_raw(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            folder=Path(temporary)
            for name in ('raw','parsed','normalized'):(folder/name).mkdir()
            job={'dataset':'institutional_market','market':'TWSE','period':'2026-09-07','segment':''}
            key='institutional_market_TWSE_2026-09-07'
            save_json(folder/'jobs.json',[job])
            save_json(folder/'parsed'/f'{key}.json',{'status':'verified'})
            raw=b'offline fixture: no network data'
            (folder/'raw'/'fixture.bin').write_bytes(raw)
            data=job|{'source':{'cache_key':'fixture','sha256':hashlib.sha256(raw).hexdigest()},
                      'records':[{'institution_type':'test','values':{'buy_amount':'10','sell_amount':'4','net_amount':'7'}}]}
            save_json(folder/'normalized'/f'{key}.json',data)
            result=audit(folder)
            self.assertFalse(result['complete'])
            self.assertEqual(result['issues'][0]['reason'],'buy_minus_sell_not_net')
            (folder/'raw'/'fixture.bin').write_bytes(b'changed')
            result=audit(folder)
            self.assertEqual(result['issues'][0]['reason'],'raw_integrity_mismatch')

    def test_monthly_archive_heading_must_match_requested_month(self):
        job=Job('revenue','TWSE','2021-10','https://mopsov.twse.com.tw/example')
        html='<h2>110年10月份</h2><table><tr>'+''.join('<td>'+v+'</td>' for v in ['2330','台積電','1,000','900','800','11.11','25','9000','8000','12.5',''])+'</tr></table>'
        result=inspect(job,{'http_status':200},html.encode('cp950'))
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['records'][0]['values']['revenue'],'1000')
        wrong=inspect(Job('revenue','TWSE','2021-11',job.url),{'http_status':200},html.encode('cp950'))
        self.assertEqual(wrong['status'],'pending')


if __name__=='__main__':unittest.main()
