"""Event-id keyed JSON with a field legend, below the Bridge 10 MB file ceiling."""
FIELDS=('session','eps_forecast','eps_actual','eps_surprise_pct','prev_close','gap_pct','day1_pct','day5_pct',
        'day1_volume_ratio','reaction_status','reaction_date','reaction_day_assumed','day5_reason',
        'day1_volume_ratio_reason','revisions','source_ticker','event_status',
        'eps_refresh_pending','eps_refresh_attempted_at','eps_status_override','fetched_at_override',
        'code','board_meeting_date','result_type')

def pack(doc):
    if 'events_by_id' in doc:return doc
    items={};tickers={};timestamps={}
    source=doc.get('source','NASDAQ' if doc['market']=='US' else 'HKEX_BMN')
    for event in doc['events']:
        eid=event['event_id'];sid,day=eid.split(':',1)
        if event.get('ticker'):tickers[sid]=event['ticker']
        if event.get('fetched_at') and day not in timestamps:timestamps[day]=event['fetched_at']
        values=[]
        for field in FIELDS:
            if field=='source_ticker' and event.get(field)==event.get('ticker'):value=None
            elif field=='eps_status_override':
                default='AVAILABLE' if event.get('eps_actual') is not None else 'EPS_ACTUAL_UNAVAILABLE'
                value=event.get('eps_status') if event.get('eps_status')!=default else None
            elif field=='fetched_at_override':value=event.get('fetched_at') if event.get('fetched_at')!=timestamps.get(day) else None
            elif field=='revisions':value=event.get('revisions') or None
            else:value=event.get(field)
            values.append(value)
        while values and values[-1] in (None,[],False):values.pop()
        if eid in items:raise ValueError('DUPLICATE_EVENT_ID:'+eid)
        items[eid]=values
    return {k:v for k,v in doc.items() if k!='events'}|{'event_format':'EVENT_ID_FIELD_ARRAY_V2',
        'event_fields':list(FIELDS),'event_count':len(items),'ticker_by_security_id':tickers,
        'fetched_at_by_report_date':timestamps,'source':source,'events_by_id':items}

def unpack(doc,active=None):
    if 'events' in doc:return doc
    fields=doc['event_fields'];events=[];tickers=doc.get('ticker_by_security_id',{});timestamps=doc.get('fetched_at_by_report_date',{})
    for eid,values in doc['events_by_id'].items():
        sid,day=eid.split(':',1)
        event={'event_id':eid,'security_id':sid,'report_date':day}
        event.update({fields[i]:val for i,val in enumerate(values) if val is not None})
        if 'eps_status_override' in event:event['eps_status']=event.pop('eps_status_override')
        else:event['eps_status']='AVAILABLE' if event.get('eps_actual') is not None else 'EPS_ACTUAL_UNAVAILABLE'
        if 'fetched_at_override' in event:event['fetched_at']=event.pop('fetched_at_override')
        elif day in timestamps:event['fetched_at']=timestamps[day]
        if doc['market']=='US':
            ticker=tickers.get(sid) or (active[sid]['ticker'] if active and sid in active else None)
            if ticker:event['ticker']=ticker
            event.setdefault('source_ticker',ticker)
        event.setdefault('revisions',[])
        event.setdefault('source',doc.get('source','NASDAQ' if doc['market']=='US' else 'HKEX_BMN'))
        if not event.get('reaction_status'):
            event['reaction_status']='COMPLETE' if all(event.get(k) is not None for k in ('gap_pct','day1_pct','day5_pct','day1_volume_ratio')) else 'PARTIAL'
        events.append(event)
    return {k:v for k,v in doc.items() if k not in ('events_by_id','event_fields','event_format','event_count','ticker_by_security_id','fetched_at_by_report_date')}|{'events':events}
