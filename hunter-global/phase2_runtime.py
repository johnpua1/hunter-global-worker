"""Hunter Phase 2 incremental earnings and industry enrichment, invoked by existing DAILY."""
from __future__ import annotations
import datetime as dt
import html as html_mod
import json
import logging
import re
import time
from collections import defaultdict
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import requests
from runner import Drive, compact, digest, parse_lines_gz
from derived import read_files
from analytics import compose, split_adjust
from foundation import current_universe, daily_segments
from phase2_pack import pack,unpack

LOG=logging.getLogger(__name__)
MYT=ZoneInfo('Asia/Kuala_Lumpur');ET=ZoneInfo('America/New_York')
BROWSER='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'

def normalize(market,ticker):
    if market=='US':return re.sub(r'[./-]','',str(ticker).strip().upper())
    match=re.search(r'(?<!\d)(\d{1,5})(?:\.HK)?\b',str(ticker).upper())
    return match.group(1).zfill(5) if match else None

def active_index(market,universe):
    active={row['security_id']:row for row in universe if row.get('listing_status')=='ACTIVE'}
    mapping=defaultdict(set)
    for sid,row in active.items():mapping[normalize(market,row['ticker'])].add(sid)
    return active,mapping

def resolve(index,market,ticker,day=None):
    ids=set(index.get(normalize(market,ticker),set()))
    if market=='US' and normalize(market,ticker)=='FI' and day and day<'2025-11-11' and 'US-001945' in set().union(*index.values()):
        ids.add('US-001945')
    return next(iter(ids)) if len(ids)==1 else None

def request(url,accept='application/json',sec=False):
    headers={'User-Agent':'Hunter Phase2 contact jkpua1@gmail.com' if sec else BROWSER,
             'Accept':accept,'Accept-Language':'en-US,en;q=0.9'}
    error=None
    for attempt in range(4):
        try:
            r=requests.get(url,headers=headers,timeout=25)
            if r.status_code in (401,403,404,429) or r.status_code>=500:
                raise RuntimeError('HTTP_'+str(r.status_code))
            r.raise_for_status()
            return r
        except (requests.RequestException,RuntimeError) as exc:
            error=type(exc).__name__+':'+str(exc)[:90]
            if attempt<3:time.sleep(2**attempt)
    raise RuntimeError('SOURCE_UNAVAILABLE:'+url.split('?')[0]+':'+str(error))

def nasdaq_rows(day):
    result=request('https://api.nasdaq.com/api/calendar/earnings?date='+day).json()
    if result.get('status',{}).get('rCode')!=200:raise ValueError('NASDAQ_STATUS_ERROR:'+day)
    data=result.get('data')
    if data is None:
        messages=result.get('status',{}).get('bCodeMessage') or []
        if any(x.get('code')==1002 and 'No record found' in x.get('errorMessage','') for x in messages):return []
        raise ValueError('NASDAQ_UNKNOWN_NULL:'+day)
    if not isinstance(data.get('rows'),list):raise ValueError('NASDAQ_ROWS_INVALID:'+day)
    return data['rows']

def numeric(value):
    if value is None:return None
    s=str(value).strip().replace('$','').replace(',','').replace('%','')
    if s in ('','N/A','--','-'):return None
    if s.startswith('(') and s.endswith(')'):s='-'+s[1:-1]
    try:return float(s)
    except ValueError:return None

def nth_weekday(year,month,weekday,n):
    first=dt.date(year,month,1)
    return first+dt.timedelta(days=(weekday-first.weekday())%7+7*(n-1))

def last_weekday(year,month,weekday):
    last=(dt.date(year+1,1,1) if month==12 else dt.date(year,month+1,1))-dt.timedelta(days=1)
    return last-dt.timedelta(days=(last.weekday()-weekday)%7)

def easter(year):
    a=year%19;b=year//100;c=year%100;d=b//4;e=b%4;f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30;i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7;m=(a+11*h+22*l)//451
    return dt.date(year,(h+l-7*m+114)//31,(h+l-7*m+114)%31+1)

def observed(day):
    if day.weekday()==5:return day-dt.timedelta(days=1)
    if day.weekday()==6:return day+dt.timedelta(days=1)
    return day

def us_holidays(year):
    fixed=[dt.date(year,1,1),dt.date(year,6,19),dt.date(year,7,4),dt.date(year,12,25)]
    return {observed(x) for x in fixed}|{nth_weekday(year,1,0,3),nth_weekday(year,2,0,3),
      easter(year)-dt.timedelta(days=2),last_weekday(year,5,0),nth_weekday(year,9,0,1),nth_weekday(year,11,3,4)}

def future_us_days(today):
    banned=set().union(*(us_holidays(y) for y in range(today.year,today.year+2)))
    return [today+dt.timedelta(days=n) for n in range(96)
            if (today+dt.timedelta(days=n)).weekday()<5 and today+dt.timedelta(days=n) not in banned]

def us_calendar(active,index,today):
    now=dt.datetime.now(MYT).isoformat(timespec='seconds');events=[];errors=[];ambiguous=[]
    for day in future_us_days(today):
        try:rows=nasdaq_rows(str(day))
        except Exception as exc:errors.append({'date':str(day),'reason':str(exc)});continue
        for raw in rows:
            ticker=raw.get('symbol','');ids=index.get(normalize('US',ticker),set())
            if len(ids)>1:ambiguous.append({'ticker':ticker,'date':str(day)});continue
            if not ids:continue
            sid=next(iter(ids));session={'time-pre-market':'BMO','time-after-hours':'AMC','time-not-supplied':'UNKNOWN'}.get(raw.get('time'),'UNKNOWN')
            row={'security_id':sid,'ticker':active[sid]['ticker'],'report_date':str(day),'session':session,
                 'eps_forecast':numeric(raw.get('epsForecast')),'source':'NASDAQ','fetched_at':now}
            if session=='UNKNOWN':row.update(report_date_myt=str(day),report_time_myt=None,time_reason='UNKNOWN_SOURCE_TIME')
            else:
                boundary=dt.datetime(day.year,day.month,day.day,9 if session=='BMO' else 16,30 if session=='BMO' else 0,tzinfo=ET).astimezone(MYT)
                row.update(report_date_myt=boundary.date().isoformat(),report_time_myt={'window':'BEFORE_OPEN' if session=='BMO' else 'AFTER_CLOSE','boundary_myt':boundary.isoformat(timespec='minutes'),'precision':'SESSION_WINDOW_ONLY'})
            events.append(row)
    unique={}
    for row in events:unique.setdefault(row['security_id']+':'+row['report_date'],row)
    events=sorted(unique.values(),key=lambda row:(row['report_date_myt'],row['ticker']))
    ids={row['security_id'] for row in events}
    return {'market':'US','window_start':str(today),'window_end':str(today+dt.timedelta(days=95)),
            'fetched_at':now,'events':events,'security_status':[{'security_id':sid,'status':'NO_DATE_IN_WINDOW' if not errors else 'DATE_UNKNOWN_SOURCE_FAILURE'} for sid in active if sid not in ids],
            'source_errors':errors,'identity_ambiguous':ambiguous}

class TableRows(HTMLParser):
    def __init__(self,target_table=None):
        super().__init__();self.rows=[];self.row=None;self.cell=None;self.target_table=target_table;self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='table' and self.target_table:
            if self.depth:self.depth+=1
            elif dict(attrs).get('id')==self.target_table:self.depth=1
        if self.target_table and not self.depth:return
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag=='table' and self.target_table and self.depth:self.depth-=1
        if self.target_table and not self.depth:return
        if tag in ('td','th') and self.row is not None and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None

def hk_calendar(active,index,today):
    now=dt.datetime.now(MYT).isoformat(timespec='seconds');errors=[];events=[];ambiguous=[]
    try:source=request('https://www3.hkexnews.hk/reports/bmn/ebmn.htm','text/html').text
    except Exception as exc:source='';errors.append({'source':'HKEX_BMN','reason':str(exc)})
    parser=TableRows();parser.feed(source)
    if source and not any(cells and cells[0]=='BM Date' for cells in parser.rows):errors.append({'source':'HKEX_BMN','reason':'TABLE_SHAPE_UNKNOWN'})
    for cells in parser.rows:
        if len(cells)<6:continue
        purpose=cells[4].upper()
        if not any(x in purpose for x in ('FIN RES','INT RES','ANNUAL RES','QUARTER RES','QUARTERLY RES','RESULTS')):continue
        try:date=dt.datetime.strptime(cells[0],'%d/%m/%Y').date()
        except ValueError:continue
        if date<today:continue
        code=normalize('HK',cells[3]);ids=index.get(code,set())
        if len(ids)>1:ambiguous.append({'code':code,'date':str(date)});continue
        if not ids:continue
        sid=next(iter(ids));events.append({'security_id':sid,'code':code,'board_meeting_date':str(date),
            'result_type':purpose,'session':'UNKNOWN','report_date_myt':str(date),'source':'HKEX_BMN','fetched_at':now})
    unique={}
    for row in events:unique.setdefault(row['security_id']+':'+row['board_meeting_date'],row)
    events=sorted(unique.values(),key=lambda row:(row['report_date_myt'],row['code']))
    ids={row['security_id'] for row in events}
    return {'market':'HK','fetched_at':now,'events':events,
            'security_status':[{'security_id':sid,'status':'NO_DATE_ANNOUNCED' if not errors else 'DATE_UNKNOWN_SOURCE_FAILURE'} for sid in active if sid not in ids],
            'source_errors':errors,'identity_ambiguous':ambiguous}

def sector_us(active,index,target,prior=None):
    now=dt.datetime.now(MYT).isoformat(timespec='seconds');prior=prior or {}
    errors=[];ambiguous=[];rows=[]
    try:
        raw=request('https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true').json()
        screener=(raw.get('data') or {}).get('rows')
        if not isinstance(screener,list) or len(screener)<5000:raise ValueError('NASDAQ_SCREENER_INCOMPLETE')
    except Exception as exc:screener=[];errors.append({'source':'NASDAQ','reason':str(exc)})
    lookup=defaultdict(list)
    for row in screener:lookup[normalize('US',row['symbol'])].append(row)
    need=[active[sid]['ticker'] for sid in target if len(lookup[normalize('US',active[sid]['ticker'])])!=1 or not lookup[normalize('US',active[sid]['ticker'])][0].get('sector') or not lookup[normalize('US',active[sid]['ticker'])][0].get('industry')]
    try:
        secs=request('https://www.sec.gov/files/company_tickers_exchange.json',sec=True).json()
        cik={normalize('US',r[2]):r[0] for r in secs['data']}
    except Exception as exc:cik={};errors.append({'source':'SEC_TICKERS','reason':str(exc)})
    sic={}
    for ticker in need:
        number=cik.get(normalize('US',ticker))
        if not number:continue
        time.sleep(0.12)  # SEC limit: at most 10 requests per second.
        try:
            doc=request(f'https://data.sec.gov/submissions/CIK{number:010d}.json',sec=True).json()
            if doc.get('sic'):sic[normalize('US',ticker)]=doc
            else:errors.append({'ticker':ticker,'reason':'SEC_SIC_UNAVAILABLE'})
        except Exception as exc:errors.append({'ticker':ticker,'reason':str(exc)})
    for sid in target:
        ticker=active[sid]['ticker'];key=normalize('US',ticker);source_rows=lookup.get(key,[])
        if len(index.get(key,set()))>1 or len(source_rows)>1:ambiguous.append({'ticker':ticker,'security_id':sid});source_rows=[]
        src=source_rows[0] if source_rows else {}
        sector=src.get('sector');industry=src.get('industry');source='NASDAQ' if sector and industry else None
        sec_doc=sic.get(key) if not source else None
        sic_code=None
        if sec_doc:
            sic_code=str(sec_doc['sic']);industry=sec_doc.get('sicDescription') or industry;source='SEC_SIC'
        reason=None if source else ('SOURCE_UNAVAILABLE' if errors else 'NO_SOURCE_MATCH')
        row={'security_id':sid,'ticker':ticker,'sector':sector or 'UNCLASSIFIED','industry':industry or 'UNCLASSIFIED',
             'sic_code':sic_code,'source':source,'reason':reason,'fetched_at':now,'changes':[]}
        old=prior.get(sid)
        if old:
            row['changes']=list(old.get('changes',[]))
            if (old.get('sector'),old.get('industry'),old.get('sic_code'))!=(row['sector'],row['industry'],row['sic_code']):
                row['changes'].append({'old':{'sector':old.get('sector'),'industry':old.get('industry'),'sic_code':old.get('sic_code')},
                    'new':{'sector':row['sector'],'industry':row['industry'],'sic_code':row['sic_code']},'discovered_at':now})
        rows.append(row)
    return rows,errors,ambiguous

def sector_hk(active,index,target,prior=None):
    now=dt.datetime.now(MYT).isoformat(timespec='seconds');prior=prior or {}
    errors=[];ambiguous=[];members=defaultdict(list)
    try:
        overview=request('https://www.aastocks.com/en/stocks/market/industry/top-industries.aspx','text/html').text
        detail=request('https://www.aastocks.com/en/stocks/market/industry/industry-performance.aspx','text/html').text
        codes=sorted(set(re.findall(r'industrysymbol=(\d{6})',overview+detail)))
        if len(codes)<100:raise ValueError('HSICS_CODES_INCOMPLETE')
    except Exception as exc:codes=[];errors.append({'source':'AASTOCKS_OVERVIEW','reason':str(exc)})
    for code in codes:
        time.sleep(1.5)
        url='https://www.aastocks.com/en/stocks/market/industry/sector-industry-details.aspx?industrysymbol='+code
        try:
            page=request(url,'text/html').text
            title=re.search(r'<title[^>]*>(.*?)</title>',page,re.S|re.I)
            if not title:raise ValueError('MISSING_INDUSTRY_TITLE')
            label=' '.join(html_mod.unescape(re.sub(r'<[^>]+>',' ',title.group(1))).split())
            match=re.search(r'Industry Details\s*-\s*(.*?)\s*-\s*(.*?)\s*$',label,re.I)
            if not match:raise ValueError('MISSING_INDUSTRY_CLASSIFICATION')
            subsector,industry=match.groups()
            parser=TableRows('tblTS2');parser.feed(page)
            if not parser.rows:raise ValueError('MISSING_CONSTITUENT_TABLE')
            for cells in parser.rows:
                if not cells:continue
                found=re.search(r'(?<!\d)(\d{5})\.HK',cells[0])
                if found:members[found.group(1)].append((code,industry,subsector))
        except Exception as exc:errors.append({'code':code,'reason':str(exc)})
    rows=[]
    for sid in target:
        code=normalize('HK',active[sid]['ticker']);found=members.get(code,[])
        if len(index.get(code,set()))>1 or len(found)>1:ambiguous.append({'code':code,'security_id':sid});found=[]
        if found:
            symbol,industry,subsector=found[0]
            row={'security_id':sid,'code':code,'industry_symbol':symbol,'hsics_industry':industry,
                 'hsics_sector':None,'hsics_subsector':subsector,'source':'AASTOCKS_HSICS','fetched_at':now,'changes':[]}
        else:row={'security_id':sid,'code':code,'hsics_industry':'UNCLASSIFIED','hsics_sector':None,
                  'hsics_subsector':None,'source':None,'reason':'INDUSTRY_SOURCE_INCOMPLETE' if errors else 'NOT_IN_HSICS',
                  'fetched_at':now,'changes':[]}
        old=prior.get(sid)
        if old:
            row['changes']=list(old.get('changes',[]))
            keys=('hsics_industry','hsics_sector','hsics_subsector')
            if tuple(old.get(k) for k in keys)!=tuple(row.get(k) for k in keys):
                row['changes'].append({'old':{k:old.get(k) for k in keys},'new':{k:row.get(k) for k in keys},'discovered_at':now})
        rows.append(row)
    return rows,errors,ambiguous

def update_history(market,old_calendar,new_calendar,history,today,refresh_eps=True):
    """Preserve event IDs; append events and revisions before replacing a calendar."""
    events=history.setdefault('events',[])
    byid={e['event_id']:e for e in events}
    old=old_calendar.get('events',[]);new=new_calendar.get('events',[])
    old_by_sid=defaultdict(list);new_by_sid=defaultdict(list)
    key=lambda row:row['report_date'] if market=='US' else row['board_meeting_date']
    for row in old:old_by_sid[row['security_id']].append(row)
    for row in new:new_by_sid[row['security_id']].append(row)
    now=dt.datetime.now(MYT).isoformat(timespec='seconds')
    for source in old:
        sid=source['security_id'];day=key(source);eid=sid+':'+day
        revised=[row for row in new_by_sid[sid] if key(row)!=day] if not any(key(x)==day for x in new_by_sid[sid]) else []
        if day>=today.isoformat() and not revised:continue
        record=byid.get(eid)
        if not record:
            record={'event_id':eid,'security_id':sid,'report_date':day,'session':source['session'],
                    'source':source['source'],'fetched_at':source['fetched_at'],'revisions':[]}
            if market=='US':
                record.update(ticker=source['ticker'],eps_forecast=source.get('eps_forecast'),
                              eps_actual=None,eps_surprise_pct=None,eps_status='EPS_ACTUAL_UNAVAILABLE',
                              eps_refresh_pending=day<today.isoformat())
            else:record.update(code=source['code'],board_meeting_date=day,result_type=source['result_type'])
            events.append(record);byid[eid]=record
        for row in revised:
            change={'old_date':day,'new_date':key(row),'discovered_at':now}
            if not any(x.get('old_date')==day and x.get('new_date')==key(row) for x in record['revisions']):
                record['revisions'].append(change);record['event_status']='REVISED'
    if market=='US' and refresh_eps:
        bydate=defaultdict(list)
        for event in events:
            if event['report_date']<today.isoformat() and event.get('eps_actual') is None and event.get('eps_refresh_pending'):
                bydate[event['report_date']].append(event)
        for day,targets in bydate.items():
            try:rows=nasdaq_rows(day)
            except Exception as exc:
                history.setdefault('source_errors',[]).append({'date':day,'reason':str(exc)})
                continue
            lookup={normalize('US',r.get('symbol','')):r for r in rows}
            for event in targets:
                raw=lookup.get(normalize('US',event['ticker']))
                if not raw and event['security_id']=='US-001945' and day<'2025-11-11':raw=lookup.get('FI')
                if raw:
                    actual=numeric(raw.get('eps'))
                    if actual is not None:
                        event.update(eps_actual=actual,eps_surprise_pct=numeric(raw.get('surprise')),
                                     eps_status='AVAILABLE',eps_refresh_pending=False)
                    else:
                        event['eps_refresh_pending']=True
                else:
                    event['eps_refresh_pending']=True
                event['eps_refresh_attempted_at']=now
    events.sort(key=lambda row:(row['report_date'],row['security_id']))
    assert len(byid)==len(events)
    return history

def compose_prices(drive,market,wanted,daily_rows=None):
    from runner import load_market
    state=load_market(drive,market);base=defaultdict(list);patches=defaultdict(list);daily=defaultdict(list)
    for batch in range(1,state.checkpoint['total_batches']+1):
        for row in parse_lines_gz(drive.read(f'{market}/BASE/batch-{batch:04d}.ndjson.gz')):
            if row['security_id'] in wanted:base[row['security_id']].append(row)
    for path in read_files(drive,market,'REPAIR_PATCH','.json'):
        doc=drive.json(path)
        items=doc.get('items') if isinstance(doc,dict) else None
        if not isinstance(items,list):items=[doc] if isinstance(doc,dict) else []
        for item in items:
            if item.get('security_id') in wanted:patches[item['security_id']].append(item)
    days = daily_segments(drive,market)
    if daily_rows is None:
        for day in days:
            LOG.info('PHASE2_DAILY_READ market=%s date=%s',market,day)
            for path in read_files(drive,market,'DAILY/'+day,('.ndjson.gz','.ndjson.gzip')):
                for row in parse_lines_gz(drive.read(path)):
                    if row['security_id'] in wanted:daily[row['security_id']].append(row)
    else:
        for sid in wanted:
            daily[sid].extend(daily_rows.get(sid,[]))
        LOG.info('PHASE2_DAILY_REUSED market=%s rows=%d',market,sum(map(len,daily.values())))
    actions=[]
    for path in read_files(drive,market,'CORPORATE_ACTIONS','.json'):actions.extend(drive.json(path))
    return {sid:{row.get('trade_date',row.get('date')):row for row in split_adjust(compose(base[sid],patches[sid],daily[sid]),actions)} for sid in wanted},sorted(set(state.calendar)|set(days))

def calculate_reactions(drive,market,history,daily_rows=None):
    events=history.get('events',[])
    targets=[e for e in events if e.get('event_status')!='REVISED' and (not e.get('reaction_status') or e.get('reaction_status') in ('PENDING_PRICE','PARTIAL'))]
    if not targets:return history
    prices,sessions=compose_prices(drive,market,{e['security_id'] for e in targets},daily_rows=daily_rows)
    for event in targets:
        sid=event['security_id'];day=event['report_date'];session=event['session']
        choices=[i for i,s in enumerate(sessions) if (s>=day if market=='US' and session=='BMO' else s>day)]
        if not choices:
            event.update(reaction_status='PENDING_PRICE',prev_close=None,gap_pct=None,day1_pct=None,day5_pct=None,day1_volume_ratio=None)
            continue
        i=choices[0];reaction=sessions[i];before=prices[sid].get(sessions[i-1]) if i>0 else None;bar=prices[sid].get(reaction)
        close=before.get('close') if before else None
        event['reaction_date']=reaction;event['prev_close']=close
        if market=='US' and session=='UNKNOWN':event['reaction_day_assumed']=True
        if not close or not bar or close<=0:
            event.update(reaction_status='PRICE_MISSING',gap_pct=None,day1_pct=None,day5_pct=None,day1_volume_ratio=None)
            continue
        event.update(gap_pct=bar['open']/close-1,day1_pct=bar['close']/close-1)
        window=[prices[sid].get(date) for date in sessions[max(0,i-20):i]]
        volumes=[float(x['volume']) for x in window if x and x.get('volume') is not None]
        avg=sum(volumes)/20 if len(volumes)==20 else None
        event['day1_volume_ratio']=bar['volume']/avg if avg and avg>0 else None
        if event['day1_volume_ratio'] is None:event['day1_volume_ratio_reason']='INSUFFICIENT_20_SESSION_VOLUME' if len(volumes)<20 else 'ZERO_BASELINE_VOLUME'
        event['day5_pct']=None
        if i+5>=len(sessions):event['day5_reason']='PENDING_FIVE_SESSIONS'
        else:
            row=prices[sid].get(sessions[i+5])
            if row:event['day5_pct']=row['close']/close-1;event.pop('day5_reason',None)
            else:event['day5_reason']='PRICE_MISSING'
        event['reaction_status']='COMPLETE' if event['day5_pct'] is not None and event['day1_volume_ratio'] is not None else 'PARTIAL'
    return history

def daily(drive:Drive,market:str,daily_rows=None):
    sector_path=f'{market}/PHASE2/SECTOR_MAP.json'
    calendar_path=f'{market}/PHASE2/EARNINGS_CALENDAR.json'
    history_path=f'{market}/PHASE2/EARNINGS_HISTORY.json'
    if not all(drive.file(p) for p in (sector_path,calendar_path,history_path)):
        LOG.info('phase2 daily skipped until initial snapshot exists market=%s',market);return {'status':'INITIAL_SNAPSHOT_PENDING'}
    universe=current_universe(drive,market)
    active,index=active_index(market,universe)
    sector_raw=drive.read(sector_path);prior=json.loads(sector_raw)
    prior_rows={row['security_id']:row for row in prior['rows']}
    today=dt.datetime.now(MYT).date()
    full=today.weekday()==0
    target=set(active) if full else set(active)-set(prior_rows)
    dropped=set(prior_rows)-set(active)
    if target or dropped:
        new_rows,errors,ambiguous=(sector_us(active,index,target,prior_rows) if market=='US'
                                   else sector_hk(active,index,target,prior_rows))
        if errors:
            new_rows=[{**prior_rows[row['security_id']], 'source_refresh_error':errors}
                      if row.get('source') is None and row['security_id'] in prior_rows and prior_rows[row['security_id']].get('source')
                      else row for row in new_rows]
        if full:prior['rows']=new_rows
        else:prior['rows']=[row for row in prior['rows'] if row['security_id'] in active]+new_rows
        prior.update(fetched_at=dt.datetime.now(MYT).isoformat(timespec='seconds'),source_errors=errors,identity_ambiguous=ambiguous)
        assert len(prior['rows'])==len(active) and {r['security_id'] for r in prior['rows']}==set(active)
        drive.put(sector_path,compact(prior),expected_sha=digest(sector_raw))
    calendar_raw=drive.read(calendar_path);old_calendar=json.loads(calendar_raw)
    new_calendar=us_calendar(active,index,today) if market=='US' else hk_calendar(active,index,today)
    history_raw=drive.read(history_path);history=unpack(json.loads(history_raw),active)
    history=update_history(market,old_calendar,new_calendar,history,today)
    LOG.info('PHASE2_STAGE market=%s stage=reactions',market)
    history=calculate_reactions(drive,market,history,daily_rows=daily_rows)
    assert len({e['event_id'] for e in history['events']})==len(history['events'])
    assert all(e['security_id'] in active for e in history['events'])
    drive.put(history_path,compact(pack(history)),expected_sha=digest(history_raw))
    drive.put(calendar_path,compact(new_calendar),expected_sha=digest(calendar_raw))
    return {'market':market,'calendar_events':len(new_calendar['events']),
            'history_events':len(history['events']),'sectors_refreshed':len(target)}
