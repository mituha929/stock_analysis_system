"""Bounded, resumable public-report history collection; no application or DB imports.

Only an explicit --live sends requests. Requests are serial, cached by full URL
and body, rate limited, never automatically retried, and stop a host on blocking.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT / 'tests' / 'output' / 'history_20261105'
TAIPEI = timezone(timedelta(hours=8))
LIMIT = 25_000_000
SECURITY_CODE = re.compile(r'(?:[0-9]{4,6}|[0-9]{5}[A-Z])')


@dataclass(frozen=True)
class Job:
    dataset: str
    market: str
    period: str
    url: str
    parameters: dict | None = None
    segment: str = ''

    @property
    def key(self):
        return '_'.join(filter(None, (self.dataset, self.market, self.period, self.segment)))


def month_periods(last: str, count=60):
    year, month = map(int, last.split('-'))
    end = year * 12 + month - 1
    return [f'{n//12:04d}-{n%12+1:02d}' for n in range(end-count+1, end+1)]


def quarter_periods(last: str, count=20):
    year, quarter = map(int, last.split('-Q'))
    end = year*4 + quarter-1
    return [f'{n//4:04d}-Q{n%4+1}' for n in range(end-count+1, end+1)]


def daily_jobs(day):
    compact = day.replace('-', '')
    for market in ('TWSE','TPEx'):
        if market == 'TWSE':
            definitions = (
                ('institutional_stock', '/fund/T86', {'date':compact, 'selectType':'ALLBUT0999'}),
                ('institutional_market', '/fund/BFI82U', {'dayDate':compact, 'type':'day'}),
                ('margin', '/marginTrading/MI_MARGN', {'date':compact, 'selectType':'ALL'}),
            )
            for dataset, path, params in definitions:
                yield Job(dataset, market, day, 'https://www.twse.com.tw/rwd/zh'+path+'?'+urlencode(params|{'response':'json'}))
        else:
            definitions = (
                ('institutional_stock','insti/dailyTrade',{'type':'Daily','sect':'EW'}),
                ('institutional_market','insti/summary',{'type':'Daily','prod':'0'}),
                ('margin','margin/balance',{}),
            )
            for dataset, path, params in definitions:
                yield Job(dataset, market, day, 'https://www.tpex.org.tw/www/zh-tw/'+path,
                          params|{'date':day.replace('-','/'),'response':'json'})
            # The official selector distinguishes stocks from all securities.
            # Keep both scopes instead of silently mixing them with OpenAPI.
            yield Job('institutional_market',market,day,
                      'https://www.tpex.org.tw/www/zh-tw/insti/summary',
                      {'type':'Daily','prod':'1','date':day.replace('-','/'),'response':'json'},'stocks')


def financial_jobs(period):
    year, quarter = map(int, period.split('-Q'))
    for market, code in (('TWSE','sii'),('TPEx','otc')):
        for dataset, form in (('income','t163sb04'),('balance','t163sb05')):
            yield Job(dataset,market,period,'https://mopsov.twse.com.tw/mops/web/ajax_'+form,
                      {'encodeURIComponent':'1','step':'1','firstin':'1','off':'1','isQuery':'Y',
                       'TYPEK':code,'year':str(year-1911),'season':f'{quarter:02d}'})


def revenue_jobs(period):
    year, month = map(int,period.split('-'))
    for market, code in (('TWSE','sii'),('TPEx','otc')):
        for segment in ('0','1'):
            # Exact archive links are emitted by official ajax_t21sc04_ifrs
            # for both oldest/newest boundary periods and domestic/foreign sets.
            yield Job('revenue',market,period,
                      f'https://mopsov.twse.com.tw/nas/t21/{code}/t21sc03_{year-1911}_{month}_{segment}.html',
                      None,segment)


def make_plan(start='2026-09-05', end='2026-11-04', revenue_end='2026-09', quarter_end='2026-Q3'):
    closed = set(json.loads((PROJECT/'data'/'stock_market_holidays.json').read_text(encoding='utf-8'))['closed_dates'])
    begin, finish = date.fromisoformat(start), date.fromisoformat(end)
    days = [begin+timedelta(days=i) for i in range((finish-begin).days+1)]
    trading = [d.isoformat() for d in days if d.weekday()<5 and d.isoformat() not in closed]
    jobs = [j for day in trading for j in daily_jobs(day)]
    jobs += [j for period in month_periods(revenue_end) for j in revenue_jobs(period)]
    jobs += [j for period in quarter_periods(quarter_end) for j in financial_jobs(period)]
    return jobs, {'daily_start':start,'daily_end':end,'candidate_trading_days':trading,
                  'excluded_calendar_dates':[d.isoformat() for d in days if d.isoformat() not in trading],
                  'revenue_months':month_periods(revenue_end),'financial_quarters':quarter_periods(quarter_end),
                  'calendar_note':'既有官方休市快照加週末；空回應仍記為待查，不擅自判為休市。',
                  'financial_note':'最新季可能尚未全數公告；財報為一般業，上市／上櫃公司範圍以當期報表為準。'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


class Reader:
    def __init__(self, folder, interval=2.0, offline=False, retry_network_errors=False, retry_transient_errors=False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        (self.folder/'raw').mkdir(exist_ok=True)
        self.interval = interval
        self.offline = offline
        self.retry_network_errors = retry_network_errors
        self.retry_transient_errors = retry_transient_errors
        self.previous = 0.0
        self.opener = build_opener(NoRedirect())
        self.block_file = self.folder/'blocked_hosts.json'
        self.blocked = set(json.loads(self.block_file.read_text()) if self.block_file.exists() else [])

    def read(self, job):
        body = urlencode(job.parameters).encode() if job.parameters is not None else None
        cache = hashlib.sha256(job.url.encode()+b'\n'+(body or b'')).hexdigest()
        meta_path = self.folder/'raw'/f'{cache}.json'
        raw_path = self.folder/'raw'/f'{cache}.bin'
        history=[]
        if meta_path.exists():
            cached=json.loads(meta_path.read_text(encoding='utf-8'))
            old_raw=raw_path.read_bytes()
            network_retry=((self.retry_network_errors or self.retry_transient_errors) and not old_raw
                           and cached.get('http_status') is None
                           and cached.get('error') in ('URLError','TimeoutError','OSError','ConnectionResetError'))
            server_retry=self.retry_transient_errors and cached.get('http_status') in (502,503,504)
            retry=not self.offline and (network_retry or server_retry)
            if not retry:return cached,old_raw
            history=cached.get('attempt_history',[])
            old_body=f'{cache}.attempt{len(history)+1}.bin'
            (self.folder/'raw'/old_body).write_bytes(old_raw)
            previous={k:v for k,v in cached.items() if k!='attempt_history'}
            previous['body_file']='raw/'+old_body
            history=history+[previous]
        if self.offline:
            return {'source_url':job.url,'error':'not_in_cache','http_status':None}, b''
        host = urlsplit(job.url).hostname
        if host in self.blocked:
            return {'http_status':None,'error':'host_stopped_after_block','source_url':job.url}, b''
        delay = self.interval-(time.monotonic()-self.previous)
        if delay>0: time.sleep(delay)
        self.previous=time.monotonic()
        meta={'source_url':job.url,'method':'POST' if body is not None else 'GET',
              'parameters':job.parameters,'fetched_at':datetime.now(TAIPEI).isoformat(),
              'http_status':None,'content_type':None,'error':None,'cache_key':cache}
        if history:meta['attempt_history']=history
        raw=b''
        try:
            req=Request(job.url,data=body,headers={'User-Agent':'CapitalMarketHistory/1.0 (public report history)',
                        'Accept':'application/json,text/html,text/csv;q=0.8',
                        **({'Content-Type':'application/x-www-form-urlencoded'} if body is not None else {})})
            try: response=self.opener.open(req,timeout=25)
            except HTTPError as error: response=error
            with response:
                meta['http_status']=response.code
                meta['content_type']=response.headers.get('Content-Type','')
                raw=response.read(LIMIT+1)
            if len(raw)>LIMIT:
                raw=raw[:LIMIT];meta['error']='response_size_limit'
            if meta['http_status']!=200: meta['error']=f"HTTP {meta['http_status']}"
        except (URLError,TimeoutError,OSError) as error:
            meta['error']=type(error).__name__
        if meta['http_status'] in (403,429) or any(x in raw.lower() for x in (
            b'the requested url was rejected',b'captcha challenge',b'just a moment...',b'access denied')):
            self.blocked.add(host)
            save_json(self.block_file,sorted(self.blocked))
            meta['error']=meta['error'] or 'blocking_page'
        meta['sha256']=hashlib.sha256(raw).hexdigest()
        meta['bytes']=len(raw)
        raw_path.write_bytes(raw)
        save_json(meta_path,meta)
        return meta,raw


class Tables(HTMLParser):
    """Preserve each table and its cells; do not mix different industry schemas."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables=[]; self.stack=[]; self.text=[]; self.links=[]
    def handle_starttag(self, tag, attrs):
        attr=dict(attrs)
        if tag=='table': self.stack.append({'rows':[],'row':None,'cell':None})
        elif self.stack and tag=='tr': self.stack[-1]['row']=[]
        elif self.stack and tag in ('td','th'): self.stack[-1]['cell']=[]
        if tag in ('a','iframe') and (attr.get('href') or attr.get('src')):
            self.links.append(attr.get('href') or attr.get('src'))
    def handle_data(self,data):
        self.text.append(data)
        if self.stack and self.stack[-1]['cell'] is not None: self.stack[-1]['cell'].append(data)
    def handle_endtag(self,tag):
        if not self.stack:return
        table=self.stack[-1]
        if tag in ('td','th') and table['cell'] is not None:
            if table['row'] is not None:table['row'].append(re.sub(r'\s+',' ',''.join(table['cell'])).strip())
            table['cell']=None
        elif tag=='tr' and table['row'] is not None:
            table['rows'].append(table['row']);table['row']=None
        elif tag=='table': self.tables.append(self.stack.pop()['rows'])


def decode(raw,content_type=''):
    charset=re.search(r'charset\s*=\s*["\']?([\w-]+)',content_type,re.I)
    choices=[charset.group(1)] if charset else []
    choices += ['utf-8-sig','cp950','big5']
    for encoding in choices:
        try:return raw.decode(encoding)
        except (UnicodeError,LookupError):pass
    raise ValueError('unverified_encoding')


def inspect(job,meta,raw):
    result=asdict(job)|{'status':'pending','source':meta,'row_count':0}
    if meta.get('error'):
        result.update(status='failed',reason=meta['error']);return result
    try:text=decode(raw,meta.get('content_type',''))
    except ValueError as e:result['reason']=str(e);return result
    try:
        data=json.loads(text)
    except json.JSONDecodeError:
        parser=Tables();parser.feed(text)
        result['format']='HTML'
        result['heading_text']=re.sub(r'\s+',' ',''.join(parser.text))[:1500]
        result['links']=parser.links
        result['tables']=parser.tables
        stock_rows=[r for table in parser.tables for r in table if r and SECURITY_CODE.fullmatch(r[0])]
        result['row_count']=len(stock_rows)
        result['reason']='html_requires_period_and_schema_validation'
        try: normalize_html(job,result,parser)
        except (ValueError,IndexError) as error:result.update(status='pending',reason=str(error))
        return result
    result['format']='JSON'
    result['response']=data
    rows=[]
    if isinstance(data,dict):
        tables=data.get('tables')
        if isinstance(tables,list):rows=[r for table in tables for r in table.get('data',[])]
        elif isinstance(data.get('data'),list):rows=data['data']
        result['response_date']=data.get('date')
        if data.get('stat','').lower() not in ('ok',''):
            result['reason']='source_no_data: '+str(data.get('stat'));return result
    elif isinstance(data,list): rows=data
    result['row_count']=len(rows)
    result['reason']='json_requires_period_and_schema_validation' if rows else 'empty_data'
    if rows:
        try: normalize_daily(job,result,data)
        except (ValueError,IndexError) as error:result.update(status='pending',reason=str(error))
    return result


def number(value):
    if value is None or (isinstance(value,str) and value.strip() in ('','--','-','不適用','N/A')):return None
    text=str(value).strip().replace('−','-')
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',text):raise ValueError('invalid_number: '+text[:60])
    value=Decimal(text.replace(',',''))
    if not value.is_finite():raise ValueError('nonfinite_number')
    return str(value)


def official_day(value):
    digits=re.sub(r'[-/.]','',str(value))
    if len(digits)==7:digits=str(int(digits[:3])+1911)+digits[3:]
    if not re.fullmatch(r'\d{8}',digits):raise ValueError('unrecognized_source_date')
    return date(int(digits[:4]),int(digits[4:6]),int(digits[6:])).isoformat()


def normalize_daily(job,result,data):
    if official_day(data.get('date'))!=job.period:raise ValueError('response_date_mismatch: '+str(data.get('date')))
    tables=data.get('tables') or [data]
    records=[]
    if job.dataset=='institutional_stock':
        table=tables[0];fields=table['fields'];rows=table['data']
        if job.market=='TWSE':
            indices=[fields.index(label) for label in ('外陸資買賣超股數(不含外資自營商)','投信買賣超股數','自營商買賣超股數')]
        else:
            if len(fields)!=24 or fields[0]!='代號' or fields[-1]!='三大法人買賣超股數合計':raise ValueError('tpex_institutional_schema_changed')
            indices=[4,13,22]
        names=['foreign_net_shares_excluding_foreign_dealers','investment_trust_net_shares','dealer_net_shares']
        for row in rows:
            if SECURITY_CODE.fullmatch(str(row[0])):
                records.append({'stock_code':row[0],'name':row[1].strip(),'values':{k:number(row[i]) for k,i in zip(names,indices)}})
        result['units']={k:'shares' for k in names}
    elif job.dataset=='institutional_market':
        table=tables[0]
        if len(table['fields'])!=4:raise ValueError('institutional_market_schema_changed')
        for row in table['data']:
            records.append({'institution_type':row[0].strip(),'values':dict(zip(('buy_amount','sell_amount','net_amount'),[number(v) for v in row[1:4]]))})
        result['units']={k:'TWD' for k in ('buy_amount','sell_amount','net_amount')}
        result['market_scope']=('TWSE BFI82U published scope' if job.market=='TWSE' else
                                'OTC stocks' if job.parameters.get('prod')=='1' else 'OTC securities')
    elif job.dataset=='margin':
        table=next((t for t in tables if t.get('fields',[None])[0] in ('代號','股票代號')),None)
        if not table:raise ValueError('margin_stock_table_missing')
        if job.market=='TWSE':
            if len(table['fields'])!=16:raise ValueError('twse_margin_schema_changed')
            indices=[5,6,11,12]
        else:
            if len(table['fields'])!=20 or '張' not in table['fields'][2]:raise ValueError('tpex_margin_schema_changed')
            indices=[2,6,10,14]
        names=['financing_previous_balance','financing_balance','short_previous_balance','short_balance']
        for row in table['data']:
            if SECURITY_CODE.fullmatch(str(row[0])):
                records.append({'stock_code':row[0],'name':row[1].strip(),'values':dict(zip(names,[number(row[i]) for i in indices]))})
        result['units']={k:'lots' for k in names}
        result['value_basis']='balance; previous/current stored separately, not daily change'
    if not records:raise ValueError('no_usable_records')
    result.update(status='verified',reason=None,records=records,record_count=len(records),period_provenance='response.date')


def normalize_html(job,result,parser):
    plain=re.sub(r'\s+',' ',''.join(parser.text))
    records=[];units={}
    if job.dataset in ('income','balance'):
        year,quarter=map(int,job.period.split('-Q'))
        if ('第'+('一','二','三','四')[quarter-1]+'季') not in plain:raise ValueError('quarter_label_mismatch')
        for table in parser.tables:
            if not table:continue
            header=table[0]
            if job.dataset=='income':
                wanted={'公司代號','公司名稱','營業收入','營業毛利（毛損）','基本每股盈餘（元）'}
            else:
                wanted={'公司代號','公司名稱','流動資產','資產總計','權益總計','保留盈餘','歸屬於母公司業主之權益合計'}
            if not wanted<=set(header):continue
            if len(set(header))!=len(header):raise ValueError('duplicate_financial_header')
            for field in header[2:]:units[field]='shares' if '股數' in field else 'TWD/share' if '每股' in field else 'TWD_thousand'
            for row in table[1:]:
                if not row or not SECURITY_CODE.fullmatch(row[0]):continue
                if len(row)!=len(header):raise ValueError('financial_row_width_mismatch')
                records.append({'stock_code':row[0],'name':row[1],
                                'values':{k:number(v) for k,v in zip(header[2:],row[2:])}})
        result['period_provenance']='official year/season POST parameters + response quarter label; response does not echo year'
        result['value_basis']='year_to_date' if job.dataset=='income' else 'point_in_time'
        result['fiscal_year']=year;result['fiscal_quarter']=quarter
        result['latest_quarter_partial']=job.period=='2026-Q3'
    elif job.dataset=='revenue':
        year,month=map(int,job.period.split('-'))
        if not re.search(fr'{year-1911}\s*年\s*0?{month}\s*月',plain):raise ValueError('revenue_month_label_mismatch')
        names=['revenue','previous_month_revenue','previous_year_same_month_revenue','mom_percent','yoy_percent',
               'year_to_date_revenue','previous_year_to_date_revenue','year_to_date_yoy_percent']
        for table in parser.tables:
            for row in table:
                if not row or not SECURITY_CODE.fullmatch(row[0]):continue
                if len(row)!=11:raise ValueError('revenue_row_width_mismatch')
                records.append({'stock_code':row[0],'name':row[1],'values':dict(zip(names,[number(v) for v in row[2:10]])),'note':row[10] or None})
        units={k:'percent' if k.endswith('percent') else 'TWD_thousand' for k in names}
        result['period_provenance']='archive filename + response ROC year/month heading'
        result['value_basis']='monthly; year_to_date fields separately identified'
    if not records:raise ValueError('no_general_industry_or_revenue_records')
    if len({r['stock_code'] for r in records})!=len(records):raise ValueError('duplicate_stock_codes')
    result.update(status='verified',reason=None,records=records,record_count=len(records),units=units)


def report(folder,plan,results):
    save_json(folder/'results.json',results)
    groups={}
    for r in results:
        key=r['dataset']+' / '+r['market']+(' / '+r['segment'] if r['segment'] else '')
        item=groups.setdefault(key,{'jobs':0,'rows':0,'verified':0,'failed':0})
        item['jobs']+=1;item['rows']+=r.get('record_count',0);item['verified']+=r['status']=='verified';item['failed']+=r['status']=='failed'
    lines=['# 歷史資料抓取報告','',
           '原始回應保留於 raw/；空回應、日期不符、風控頁及未解析資料不算成功。',
           '財報最新季可能尚未公告齊全；不寫正式資料庫、不啟動正式排程。','',
           '|資料／市場|已處理期間|正規化資料列|已驗證|失敗|','|---|---:|---:|---:|---:|']
    for key,g in groups.items():lines.append(f"|{key}|{g['jobs']}|{g['rows']}|{g['verified']}|{g['failed']}|")
    lines+=['','## 範圍','',json.dumps(plan,ensure_ascii=False,indent=2)]
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true')
    p.add_argument('--replay',action='store_true',help='只讀原始快取重新解析，絕不連線')
    p.add_argument('--retry-network-errors',action='store_true',help='明確補抓先前沒有 HTTP 回應的連線失敗；每份一次，保留舊紀錄，不重試 HTTP 封鎖')
    p.add_argument('--retry-transient-errors',action='store_true',help='明確補抓無回應及 HTTP 502/503/504，每份一次，保留舊回應；不重試 403/429')
    p.add_argument('--probe',action='store_true',help='只測最新及最早一期，先驗證來源歷史能力')
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--interval',type=float,default=2.0)
    p.add_argument('--groups',default='daily,revenue,financial')
    args=p.parse_args()
    if args.live and args.replay:p.error('--live 與 --replay 不可同時使用')
    if args.retry_network_errors and not args.live:p.error('--retry-network-errors 必須搭配 --live')
    if args.retry_transient_errors and not args.live:p.error('--retry-transient-errors 必須搭配 --live')
    if not math.isfinite(args.interval) or args.interval<2:p.error('間隔必須為有限數值，且至少 2 秒')
    jobs,plan=make_plan()
    if args.probe:
        jobs=list(daily_jobs(plan['candidate_trading_days'][0]))+list(daily_jobs(plan['candidate_trading_days'][-1]))
        jobs+=list(revenue_jobs(plan['revenue_months'][0]))+list(revenue_jobs(plan['revenue_months'][-1]))
        jobs+=list(financial_jobs(plan['financial_quarters'][0]))+list(financial_jobs(plan['financial_quarters'][-1]))
    groups={group.strip() for group in args.groups.split(',')}
    if not groups or not groups<={'daily','revenue','financial'}:
        p.error('--groups 只接受 daily、revenue、financial')
    jobs=[j for j in jobs if ('daily' if j.dataset in ('institutional_stock','institutional_market','margin') else 'revenue' if j.dataset=='revenue' else 'financial') in groups]
    folder=args.output;folder.mkdir(parents=True,exist_ok=True)
    save_json(folder/'plan.json',plan)
    save_json(folder/'jobs.json',[asdict(j) for j in jobs])
    if not args.live and not args.replay:
        print(f'Plan only: {len(jobs)} jobs. Add --live to fetch.');return 0
    reader=Reader(folder,args.interval,offline=args.replay,retry_network_errors=args.retry_network_errors,
                  retry_transient_errors=args.retry_transient_errors)
    results=[]
    for index,job in enumerate(jobs,1):
        meta,raw=reader.read(job)
        result=inspect(job,meta,raw)
        results.append(result)
        if result.get('records'):
            (folder/'normalized').mkdir(exist_ok=True)
            normalized={k:v for k,v in result.items() if k not in ('response','tables','links','heading_text')}
            save_json(folder/'normalized'/f'{job.key}.json',normalized)
            result['normalized_file']='normalized/'+job.key+'.json'
        summary={k:v for k,v in result.items() if k not in ('response','tables','links','records')}
        # Individual processed files support replay without loading all raw rows.
        (folder/'parsed').mkdir(exist_ok=True)
        save_json(folder/'parsed'/f'{job.key}.json',summary)
        results[-1]=summary
        report(folder,plan,results)
        print(f'{index}/{len(jobs)} {job.key}: HTTP {meta.get("http_status")} {result["status"]} rows={result["row_count"]}',flush=True)
    return 0 if all(r['status']=='verified' for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
