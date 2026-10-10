"""Bounded native sleep cards projected only from authenticated fitness reads."""
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SLEEP_GUIDANCE = (
    'For sleep graphs use fitness_read collection=sleep with the requested dates and timezone. '
    'A native interactive sleep card is attached from the tool result, including missing dates. '
    'Do not say sleep charts are unsupported or produce text bars, chart code, HTML or JSON. '
    'Give a short explanation of the observed pattern without repeating every nightly value. '
    'Oura and Samsung are separate sources; never add overlapping device sleep totals. '
    'The card selects Oura main sleep when labeled long_sleep, otherwise the longest unclassified '
    'session; Samsung shows its longest recorded session per local wake date. Naps and additional '
    'sessions are not silently added. Missing or stale records are unknown, not zero sleep. '
    'Total sleep is estimated time asleep; time in bed is the recorded interval, not sleep. '
    'Efficiency and HRV are provider estimates, not diagnoses or proof of recovery. '
    'Do not infer sleep stages, apnea, CPAP effectiveness or fitness improvement from these summaries. '
    'Compare only actual available nights; a partial range is not a complete weekly average. '
    'If the tool reports no usable sleep records, explain that without inventing a trend.'
)


def sleep_graph_request(text, timezone='America/New_York'):
    if not (re.search(r'\bsleep\b', text, re.I) and
            re.search(r'\b(graph|chart|plot|visuali[sz]e)\b', text, re.I)):
        return None
    # Complex ranges/timezones are resolved by the model, never narrowed here.
    if re.search(r'\b(compare|month|versus|vs|tomorrow|workouts?)\b|\b[A-Za-z_]+/[A-Za-z_]+\b',text,re.I):
        return None
    explicit = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text)
    try:
        today = datetime.now(ZoneInfo(timezone)).date()
        if explicit:
            if len(explicit) not in (1,2): return None
            first, last = date.fromisoformat(explicit[0]), date.fromisoformat(explicit[-1])
        elif re.search(r'\b(?:last|past)\s+(?:seven|7)\s+(?:days|nights)\b|\b(?:past)\s+week\b',text,re.I):
            if re.search(r'\blast week\b',text,re.I): return None
            first, last = today-timedelta(days=6), today
        elif re.search(r'\blast night\b', text,re.I):
            if re.search(r'\b(yesterday|today)\b',text,re.I): return None
            first = last = today
        elif re.search(r'\b(last|past|week|days|nights)\b',text,re.I):
            return None
        elif re.search(r'\byesterday\b',text,re.I):
            if re.search(r'\btoday\b',text,re.I): return None
            first = last = today-timedelta(days=1)
        elif re.search(r'\btoday\b',text,re.I): first = last = today
        else: first, last = today-timedelta(days=6), today
        if not 0 <= (last-first).days <= 30: return None
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return {'provider':'all','collection':'sleep','timezone':timezone,
            'start':first.isoformat(),'end':last.isoformat()}


def _number(value, maximum, positive=False):
    return value if (not isinstance(value,bool) and isinstance(value,(int,float))
        and math.isfinite(value) and (0 < value if positive else 0 <= value)
        and value <= maximum) else None


def _interval(start,end):
    try:
        a, b = (datetime.fromisoformat(v.replace('Z','+00:00')) for v in (start,end))
        if not a.tzinfo or not b.tzinfo: return None
        seconds = (b-a).total_seconds()
        if 0 < seconds <= 172800: return seconds
    except (ValueError,TypeError,AttributeError): pass
    return None


def build_sleep_report(result):
    try:
        first, last = (date.fromisoformat(result[k]) for k in ('start_date','end_date'))
        if not 0 <= (last-first).days <= 30: return None
        zone = ZoneInfo(result.get('timezone','America/New_York'))
    except (ValueError,KeyError,ZoneInfoNotFoundError,TypeError): return None
    days = [(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
    providers = []
    for source_id, source in result.get('sources',{}).items():
        if source_id not in ('oura','samsung_health') or not isinstance(source,dict): continue
        collection = source.get('collections',{}).get('sleep',{}) if source_id=='oura' else source
        if not isinstance(collection,dict): continue
        # daily_sleep scores alone cannot establish sleep duration or a timeline.
        if source_id=='oura' and not collection: continue
        status = collection.get('status','unavailable')
        grouped = {day:[] for day in days}
        invalid = 0
        records = collection.get('records',[])
        if not isinstance(records,list): records=[]
        for row in records[:2000] if status in ('ok','connected') else []:
            if not isinstance(row,dict): invalid+=1; continue
            if source_id=='samsung_health' and row.get('type')!='sleep': continue
            start, end = (row.get('bedtime_start'),row.get('bedtime_end')) if source_id=='oura' else (row.get('start'),row.get('end'))
            seconds = _interval(start,end)
            if seconds is None: invalid+=1; continue
            day = row.get('day') if source_id=='oura' else datetime.fromisoformat(end.replace('Z','+00:00')).astimezone(zone).date().isoformat()
            if day not in grouped: continue
            duration = _number(row.get('total_sleep_duration' if source_id=='oura' else 'duration_seconds'),172800)
            if duration is not None and duration > seconds: duration=None
            metrics = {'time_in_bed_seconds':seconds}
            if duration is not None: metrics['total_sleep_seconds']=duration
            if source_id=='oura':
                for key, field, bound, positive in (
                    ('efficiency_pct','efficiency',100,False),
                    ('heart_rate_bpm','average_heart_rate',300,True),
                    ('hrv_ms','average_hrv',1000,False)):
                    value = _number(row.get(field),bound,positive)
                    if value is not None: metrics[key]=value
            grouped[day].append({'start':start,'end':end,'metrics':metrics,
                                 'kind':row.get('type') if source_id=='oura' else None})
        nights=[]
        for day, rows in grouped.items():
            mains=[r for r in rows if r['kind']=='long_sleep']
            unknown=[r for r in rows if not r['kind']]
            candidates=mains or unknown
            selected=max(candidates,key=lambda r:r['metrics'].get('total_sleep_seconds',r['metrics']['time_in_bed_seconds']),default=None)
            night={'date':day,'metrics':{},'other_sessions':len(rows),'selection':'none'}
            if selected:
                night.update(start=selected['start'],end=selected['end'],metrics=selected['metrics'],
                             selection='main' if mains else 'longest',other_sessions=len(rows)-1)
            nights.append(night)
        providers.append({'id':source_id,'label':'Oura' if source_id=='oura' else 'Samsung Health',
            'status':status if status in ('connected','ok','failed','permission_missing') else 'unavailable',
            'nights':nights,'capped':bool(collection.get('truncated')),
            'incomplete':bool(source.get('incomplete')) or invalid>0,
            'fetched_at':source.get('fetched_at') if source_id=='oura' else None,
            'synced_at':source.get('synced_at') if source_id=='samsung_health' else None})
    return {'version':1,'timezone':str(zone),'start_date':days[0],'end_date':days[-1],
            'sources':providers} if providers else None
