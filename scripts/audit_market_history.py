"""Read saved history only: coverage, source integrity, duplicate keys and values."""
from collections import Counter, defaultdict
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path

from scripts.collect_market_history import DEFAULT_OUTPUT, save_json
from scripts.market_data_quality import MARKET_NET_TOLERANCE_TWD, market_net_difference


def audit(folder):
    folder=Path(folder)
    jobs=json.loads((folder/'jobs.json').read_text(encoding='utf-8'))
    coverage=defaultdict(lambda:{'expected':0,'verified':0,'records':0,'periods':[],'stock_codes':set()})
    issues=[];accepted_exceptions=[];integrity_checked=set();baseline_checks=[];sample_periods=defaultdict(list)
    source_hashes=defaultdict(list)
    normalized_hashes=defaultdict(list)
    for job in jobs:
        key='_'.join(filter(None,(job['dataset'],job['market'],job['period'],job['segment'])))
        group=job['dataset']+'/'+job['market']+('/'+job['segment'] if job['segment'] else '')
        c=coverage[group];c['expected']+=1
        parsed=folder/'parsed'/f'{key}.json'
        normalized=folder/'normalized'/f'{key}.json'
        if not parsed.exists():issues.append({'key':key,'reason':'not_processed'});continue
        meta=json.loads(parsed.read_text(encoding='utf-8'))
        if meta['status']!='verified' or not normalized.exists():
            issues.append({'key':key,'reason':meta.get('reason') or 'not_verified'});continue
        data=json.loads(normalized.read_text(encoding='utf-8'))
        if any(data.get(k)!=job[k] for k in ('dataset','market','period','segment')):
            issues.append({'key':key,'reason':'normalized_identity_mismatch'});continue
        source=data['source'];cache=source.get('cache_key')
        if cache not in integrity_checked:
            raw=folder/'raw'/f'{cache}.bin'
            if not raw.exists() or hashlib.sha256(raw.read_bytes()).hexdigest()!=source.get('sha256'):
                issues.append({'key':key,'reason':'raw_integrity_mismatch'});continue
            integrity_checked.add(cache)
        source_hashes[(job['dataset'],job['market'],job['segment'],source['sha256'])].append(job['period'])
        stable_records=sorted(data['records'],key=lambda r:r.get('stock_code') or r.get('institution_type') or r.get('measure',''))
        content_hash=hashlib.sha256(json.dumps(stable_records,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        normalized_hashes[(job['dataset'],job['market'],job['segment'],content_hash)].append(job['period'])
        identifiers=[]
        for record in data['records']:
            code=record.get('stock_code') or record.get('institution_type') or record.get('measure')
            identifiers.append(code)
            if record.get('stock_code'):
                c['stock_codes'].add(code)
                if code in ('2330','6488','1215','1584'):
                    sample_periods[job['dataset']+'/'+job['market']+'/'+code].append(job['period'])
            for field,value in record['values'].items():
                if value is not None:
                    try:
                        number=Decimal(value)
                    except (ArithmeticError, TypeError, ValueError):
                        issues.append({'key':key,'reason':'invalid_number','code':code,'field':field})
                        continue
                    if not number.is_finite():issues.append({'key':key,'reason':'nonfinite_value','code':code,'field':field})
                    elif job['dataset'] in ('margin','margin_market') and number<0:issues.append({'key':key,'reason':'negative_margin_balance','code':code})
            values=record['values']
            if job['dataset']=='institutional_market':
                try:
                    difference=market_net_difference(values)
                except ValueError:
                    issues.append({'key':key,'reason':'invalid_market_amount','code':code})
                else:
                    if difference:
                        accepted=abs(difference)<=MARKET_NET_TOLERANCE_TWD
                        (accepted_exceptions if accepted else issues).append({
                            'key':key,'reason':'buy_minus_sell_not_net','code':code,
                            'buy_amount':values['buy_amount'],'sell_amount':values['sell_amount'],
                            'published_net_amount':values['net_amount'],'difference_twd':str(difference),
                            'net_difference_flag':True,'accepted':accepted,
                        })
            if job['dataset']=='balance' and all(values.get(k) is not None for k in ('資產總計','負債總計','權益總計')):
                delta=Decimal(values['資產總計'])-Decimal(values['負債總計'])-Decimal(values['權益總計'])
                if abs(delta)>2:issues.append({'key':key,'reason':'balance_equation_mismatch','code':code,'difference_thousand_twd':str(delta)})
        if len(set(identifiers))!=len(identifiers):issues.append({'key':key,'reason':'duplicate_security_or_institution_key'})
        if not identifiers:issues.append({'key':key,'reason':'empty_normalized_records'});continue
        c['verified']+=1;c['records']+=len(identifiers);c['periods'].append(job['period'])
    identical_periods=[]
    for group,periods in source_hashes.items():
        if len(set(periods))>1:
            identical_periods.append({'dataset':group[0],'market':group[1],'segment':group[2],'periods':sorted(set(periods))})
    identical_normalized=[]
    for group,periods in normalized_hashes.items():
        if len(set(periods))>1:
            identical_normalized.append({'dataset':group[0],'market':group[1],'segment':group[2],'periods':sorted(set(periods))})
    # Prior independent browser verification is a separate known-value reference.
    refs={
      'revenue_TWSE_2026-09_0':('2330',{'revenue':'511857399','year_to_date_revenue':'3898726974'}),
      'revenue_TPEx_2026-09_0':('6488',{'revenue':'5128632','year_to_date_revenue':'44069861'}),
      'income_TWSE_2026-Q3':('1215',{'營業收入':'21866426','基本每股盈餘（元）':'5.47'}),
      'income_TPEx_2026-Q3':('1584',{'營業收入':'1708467','基本每股盈餘（元）':'0.40'}),
      'balance_TWSE_2026-Q3':('1215',{'資產總計':'34472391','負債總計':'22263677'}),
      'balance_TPEx_2026-Q3':('1584',{'資產總計':'8039042','負債總計':'3221127'}),
    }
    for key,(code,fields) in refs.items():
        path=folder/'normalized'/f'{key}.json'
        if not path.exists():continue
        data=json.loads(path.read_text(encoding='utf-8'))
        row=next((r for r in data['records'] if r['stock_code']==code),None)
        for field,expected in fields.items():
            value=row['values'].get(field) if row else None
            baseline_checks.append({'key':key,'code':code,'field':field,'actual':value,'expected':expected,
                                    'passed':value is not None and Decimal(value)==Decimal(expected)})
    for key,c in coverage.items():
        c['stock_count']=len(c.pop('stock_codes'));c['periods']=sorted(set(c['periods']))
        c['first_period']=min(c['periods']) if c['periods'] else None
        c['last_period']=max(c['periods']) if c['periods'] else None
    result={'expected_jobs':len(jobs),'verified_jobs':sum(c['verified'] for c in coverage.values()),
            'normalized_records':sum(c['records'] for c in coverage.values()),'raw_hashes_verified':len(integrity_checked),
            'coverage':dict(coverage),'issues':issues,'accepted_exceptions':accepted_exceptions,
            'identical_responses_for_different_periods':identical_periods,
            'identical_normalized_data_for_different_periods':identical_normalized,
            'independent_browser_reference_checks':baseline_checks,
            'sample_periods':{k:sorted(set(v)) for k,v in sample_periods.items()}}
    result['validation_policy']={'market_net_tolerance_TWD':str(MARKET_NET_TOLERANCE_TWD),'balance_equation_tolerance_thousand_TWD':'2',
                                 'note':'Original amounts are retained, including any detected source discrepancies.'}
    result['complete']=result['verified_jobs']==len(jobs) and not issues and not identical_periods and not identical_normalized and all(c['passed'] for c in baseline_checks)
    result['download_complete']=result['verified_jobs']==len(jobs)
    save_json(folder/'audit.json',result)
    lines=['# 歷史資料完整性驗收','',
           f"已驗證 {result['verified_jobs']} / {len(jobs)} 組報表；正規化資料 {result['normalized_records']:,} 筆。",
           f"原始檔 SHA-256 已核對 {len(integrity_checked)} 個。抓取完成：{result['download_complete']}；完整驗收：{result['complete']}。",'',
           '|資料／市場|預期期間|已驗證期間|正規化筆數|起訖|','|---|---:|---:|---:|---|']
    for key,c in coverage.items():lines.append(f"|{key}|{c['expected']}|{c['verified']}|{c['records']:,}|{c['first_period']}～{c['last_period']}|")
    actual_issues=[i for i in issues if i['reason']!='not_processed']
    for title, entries in (('待查資料', actual_issues), ('容差內通過並保留旗標', accepted_exceptions)):
        if entries:
            lines+=['',f'## {title}（{len(entries)} 筆）','', '|來源／期間|代號／分類|原因|差額|','|---|---|---|---|']
            for item in entries[:30]:
                lines.append(f"|{item['key']}|{item.get('code','—')}|{item['reason']}|{item.get('difference_twd',item.get('difference_thousand_twd','—'))}|")
    lines+=['', f'市場法人金額差額以 ±{MARKET_NET_TOLERANCE_TWD} 元為容差，含端點。所有非零差額保留官方值與旗標。']
    lines+=['','## 限制','',
            '- 財報為一般業；原始回應保留其他產業表格，未將其混入一般業正規化資料。',
            '- 2026-Q3 尚在公告期間，不能把當季已出表公司當成全市場均已公告。',
            '- 財報年度來自官方歷史查詢參數，回應核對季別；來源 HTML 未回顯年度，保留 period_provenance 說明。',
            '- 月營收分國內（0）、國外（1）公司，全部保留；空值不是零。',
            '- 抓取的是期間資料，不代表已取得當時公告時間或歷史修訂版本。',
            '- 未處理與異常明細見 audit.json 的 issues；跨期原文或正規化內容完全相同列為待查，來源產檔時間不同也不會掩蓋相同內容。',
            '',f"獨立瀏覽器查證值比對：{sum(c['passed'] for c in baseline_checks)} / {len(baseline_checks)}。"]
    (folder/'audit.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args()
    result=audit(args.output)
    print(json.dumps({k:result[k] for k in ('expected_jobs','verified_jobs','normalized_records','download_complete','complete')},ensure_ascii=False))
    print('Issues:',len(result['issues']),'Accepted exceptions:',len(result['accepted_exceptions']),
          'Identical raw:',len(result['identical_responses_for_different_periods']),
          'Identical normalized:',len(result['identical_normalized_data_for_different_periods']))
    raise SystemExit(0 if result['complete'] else 1)
