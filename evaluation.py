"""Cost-aware replay and chronological evaluation. All orders stay in PaperExchange.

OHLC paths and liquidity are explicit assumptions, not reconstructed order books.
Maker comparisons require recorded L2 and subsequent trades. No profitable
strategy, calibrated fills, or independent samples are presumed by these tools.
"""
import copy
import gzip
import hashlib
import html
import json
import math
import random
import sqlite3
import statistics
import time
from dataclasses import asdict,replace
from pathlib import Path
from core import Config,Store,SafetyError,validate_market
from execution import ProtectedEngine
from simulator import PaperExchange
from history import aggregate
from persistence import encode


class MemoryStore(Store):
    """Same order logic; expensive durable commits are unnecessary in offline replay."""
    def __init__(self):super().__init__(':memory:');self.state=None;self.audit=[]
    def load(self):return copy.deepcopy(self.state)
    def save(self,state,kind='tick',detail=None):
        self.state=state
        if kind!='tick':self.audit.append((kind,copy.deepcopy(detail)))


def simulated_meta(pair):
    # Explicit scenario assumptions. Live always obtains current AssetPairs.
    return {'base':'XXBT' if pair=='XBTEUR' else 'XETH','quote':'ZEUR','altname':pair,
        'wsname':'XBT/EUR' if pair=='XBTEUR' else 'ETH/EUR','lot_decimals':8,
        'pair_decimals':1,'tick_size':'0.1','ordermin':'0.00005' if pair=='XBTEUR' else '0.002',
        'costmin':'0.5','status':'online'}


def make_engine(cfg,now,meta=None):
    if cfg.mode!='paper':raise SafetyError('Historikktest tillater aldri live-modus.')
    st=MemoryStore();broker=PaperExchange(cfg,cfg.pair,meta or simulated_meta(cfg.pair),st,now,durable=False)
    engine=ProtectedEngine(cfg,st,broker)
    return engine,broker,st


def block_bootstrap(values,block=5,samples=600,seed=41):
    values=list(values)
    if len(values)<max(20,2*block):return {'n':len(values),'mean_ci95':None,'reason':'For lite datagrunnlag'}
    rng=random.Random(seed);means=[]
    for _ in range(samples):
        batch=[]
        while len(batch)<len(values):
            start=rng.randrange(len(values));batch.extend(values[(start+j)%len(values)] for j in range(block))
        means.append(statistics.fmean(batch[:len(values)]))
    means.sort()
    return {'n':len(values),'block':block,'samples':samples,'mean':statistics.fmean(values),
        'mean_ci95':[means[int(.025*samples)],means[min(samples-1,int(.975*samples))]],
        'assumption':'Moving circular blocks; interval does not correct strategy selection or regime changes'}


def reliability(outcomes,field='p'):
    rows=[r for r in outcomes if r.get('y') in (0,1) and r.get(field) is not None]
    if not rows:return {'n':0,'brier':None,'bins':[]}
    bins=[]
    for low in range(10):
        group=[r for r in rows if min(9,int(r[field]*10))==low]
        if group:bins.append({'low':low/10,'high':(low+1)/10,'n':len(group),
            'predicted':statistics.fmean(r[field] for r in group),'observed':statistics.fmean(r['y'] for r in group)})
    return {'n':len(rows),'brier':statistics.fmean((r[field]-r['y'])**2 for r in rows),
        'positive_rate':statistics.fmean(r['y'] for r in rows),'bins':bins}


def purged_splits(rows,folds=3,minimum_train=60,embargo=3600,holdout_fraction=.2):
    """Expanding windows. Training outcomes must close before validation starts."""
    ordered=sorted(rows,key=lambda r:(r['created'],r.get('id',0)))
    hold_start=int(len(ordered)*(1-holdout_fraction));development=ordered[:hold_start]
    if len(development)<minimum_train+folds*10:return [],ordered[hold_start:]
    width=max(10,(len(development)-minimum_train)//folds);splits=[]
    for f in range(folds):
        start=minimum_train+f*width;stop=len(development) if f==folds-1 else start+width
        valid=development[start:stop]
        if not valid:continue
        boundary=valid[0]['created']-embargo
        train=[r for r in development[:start] if r['closed']<boundary]
        splits.append((train,valid))
    return splits,ordered[hold_start:]


def metrics(engine,curve,exposed,elapsed,notes):
    from telemetry import execution_summary
    if not curve:raise ValueError('Ingen evaluerbare datapunkter etter oppvarming.')
    peak=engine.cfg.capital;dd=0.;daily={}
    for at,equity in curve:
        peak=max(peak,equity);dd=max(dd,1-equity/peak);daily[int(at)//86400]=equity
    previous=engine.cfg.capital;daily_changes=[]
    for value in daily.values():daily_changes.append(value-previous);previous=value
    positions=engine.s['completed_positions']
    result={'equity_eur':curve[-1][1],'net_eur':curve[-1][1]-engine.cfg.capital,
        'max_drawdown':dd,'fees_eur':engine.s['fees'],'executed_orders':engine.s['trades'],
        'completed_positions':len(positions),'positive_positions':sum(float(r['net_eur'])>0 for r in positions),
        'exposure_fraction':exposed/elapsed if elapsed else 0,'cash_benchmark_eur':0,
        'buy_hold_net_eur':engine.snapshot(*engine.api.quote(engine.cfg.pair))['hold'],
        'remaining_qty':engine.s['qty'],'halts':engine.s.get('halts',{}),
        'daily_net_bootstrap':block_bootstrap(daily_changes),'notes':notes,
        'mean_net_per_position_eur':sum(float(r['net_eur']) for r in positions)/len(positions) if positions else None,
        'daily_closing_equity':daily,'execution_quality':execution_summary(engine.s),
        'positions':positions,'curve':curve,'config':asdict(engine.cfg)}
    return result


def candle_replay(cfg,rows,source_interval=1,path='low-first',spread=.0002,liquidity_btc=1.,
                  participation=.1,delay_bars=0,start=None,end=None,metadata=None,cancel=None):
    if cfg.mode!='paper' or cfg.entry_method=='POST':
        raise ValueError('OHLC-test støtter bare papir IOC/FOK. Maker krever opptak med ordrebok og handler.')
    if path not in ('low-first','high-first') or not 0<=delay_bars<=10:raise ValueError('Ugyldig testscenario.')
    if source_interval>cfg.interval:raise ValueError('Kildedata må være minst like detaljerte som strategien.')
    rows=list(rows)
    if len(rows)<61:raise ValueError('For få prisperioder.')
    parents=list(aggregate(rows,source_interval,cfg.interval));closed=[];cursor=0
    engine,broker,store=make_engine(cfg,rows[0][0],metadata)
    curve=[];exposed=elapsed=0.;last_at=None;gaps=0;bar_gaps=0;delayed=[];last_decision_bar=None
    try:
        for index,row in enumerate(rows):
            if cancel and cancel.is_set():raise InterruptedError('Historikktesten ble stoppet.')
            at=int(row[0]);o,h,l,c=map(float,row[1:5])
            if end is not None and at>=end:break
            if index and at-rows[index-1][0]!=source_interval*60:gaps+=1
            while cursor<len(parents) and parents[cursor][0]+cfg.interval*60<=at:
                closed.append(parents[cursor]);closed=closed[-720:];cursor+=1
            if len(closed)<60 or (start is not None and at<start):continue
            history_ok=not any(b[0]-a[0]!=cfg.interval*60 for a,b in zip(closed[-60:],closed[-59:]))
            points=(o,l,h,c) if path=='low-first' else (o,h,l,c)
            for part,price in enumerate(points):
                now=at+part*source_interval*60/4
                bid=price*(1-spread/2);ask=price*(1+spread/2)
                # Candle volume limits the scenario; it does not reveal actual queue liquidity.
                available=max(0,min(liquidity_btc,float(row[6])/4))
                book={'bids':[[bid,available]],'asks':[[ask,available]]}
                was_exposed=bool(engine.s['qty'])
                broker.update(book,now,rows=closed,last=price,participation=participation)
                engine.service();engine.monitor(bid,ask,now)
                if part==0 and history_ok and closed[-1][0]!=last_decision_bar:
                    last_decision_bar=closed[-1][0];delayed.append((index+delay_bars,copy.deepcopy(closed)))
                if part==0 and delayed and delayed[0][0]<=index:
                    _,decision_rows=delayed.pop(0)
                    try:engine.tick(decision_rows,bid,ask,now)
                    except SafetyError:
                        # Stale/gapped data is never traded. Genuine order faults still fail the run.
                        try:validate_market(decision_rows,bid,ask,now,cfg.interval)
                        except SafetyError:bar_gaps+=1
                        else:raise
                if last_at is not None:
                    step=now-last_at;elapsed+=step;exposed+=step*was_exposed
                last_at=now
                curve.append([now,engine.equity(bid)])
        notes=[f'OHLC path: {path}; synthetic spread {spread:.4%}; participation {participation:.1%}.',
            f'Historical metadata is a scenario assumption: {metadata or simulated_meta(cfg.pair)}',
            f'Data gaps: {gaps}; rejected stale/gapped decisions: {bar_gaps}; signal delay: {delay_bars} source bars.',
            'Intrabar ordering, queue liquidity and market impact are unknown. Compare alternate/stressed scenarios.',
            'Ending positions are marked at estimated liquidation value, not forcibly sold. Cash benchmark earns no interest.']
        return metrics(engine,curve,exposed,elapsed,notes)
    finally:store.close()


def event_replay(cfg,files,cancel=None,entry_filter=None):
    if cfg.mode!='paper':raise SafetyError('Opptak kan bare spilles av i papirhandel.')
    engine=broker=store=None;rows=[];meta=None;book=None;last=None;trades=[];curve=[];previous=None
    exposed=elapsed=0.;last_sequence=None;last_bar=None;book_at=0.;last_received=None
    try:
        for file in files:
            with gzip.open(file,'rt',encoding='utf-8') as source:
                for line in source:
                    if cancel and cancel.is_set():raise InterruptedError('Avspilling stoppet.')
                    event=json.loads(line);kind=event['kind'];data=event.get('data',{});at=event.get('received',event['observed'])
                    if kind=='capture_gap':raise ValueError('Opptaket har et registrert hull. Testen avvises.')
                    if last_received is not None and at<last_received:raise ValueError('Mottakstid gikk bakover i opptaket.')
                    last_received=at
                    seq=event.get('sequence')
                    if seq is not None and last_sequence is not None and seq!=last_sequence+1:
                        raise ValueError('Opptaket har manglende/omordnede hendelser. Velg alle segmenter fra samme økt i rekkefølge.')
                    if seq is not None:last_sequence=seq
                    if data.get('pair') not in (None,cfg.pair):raise ValueError('Opptaket har et annet marked.')
                    if kind=='metadata':meta=data['meta']
                    if kind=='instrument' and meta:
                        for source,target in (('price_increment','tick_size'),('qty_min','ordermin'),('cost_min','costmin')):
                            if source in data:meta[target]=str(data[source])
                        if 'qty_precision' in data:meta['lot_decimals']=int(data['qty_precision'])
                        meta['status']=data['status']
                    if kind=='candles' and data['interval']==cfg.interval:rows=data['rows']
                    if kind=='trades':trades=data['trades'];last=trades[-1]['price'] if trades else last
                    if kind=='book':book=data['book'];book_at=min(at,event['observed']);last=data.get('last') or last
                    if kind=='data_error' and data.get('component') in ('quote','websocket'):
                        book=None
                    if not book or not meta or at-book_at>10 or len(rows)<60 or kind not in ('book','trades'):continue
                    if engine is None:engine,broker,store=make_engine(cfg,at,meta)
                    if previous is not None and at<previous:raise ValueError('Tid gikk bakover i opptaket.')
                    was_exposed=bool(engine.s['qty']);broker.metadata=copy.deepcopy(meta)
                    broker.update(book,at,rows,trades=trades,last=last);trades=[]
                    engine.service();bid,ask=broker.quote(cfg.pair);engine.monitor(bid,ask,at)
                    if rows[-1][0]!=last_bar:
                        try:validate_market(rows,bid,ask,at,cfg.interval)
                        except SafetyError:pass
                        else:
                            allowed=meta.get('status')=='online' and (True if entry_filter is None else bool(entry_filter(rows,bid,ask,at)))
                            engine.tick(rows,bid,ask,at,entry_allowed=allowed);last_bar=rows[-1][0]
                    if previous is not None:elapsed+=at-previous;exposed+=(at-previous)*was_exposed
                    previous=at;curve.append([at,engine.equity(bid)])
        if engine is None:raise ValueError('Opptaket mangler metadata, priser eller 60 ferdige strategiperioder.')
        return metrics(engine,curve,exposed,elapsed,['Recorded L2/trades; queue model remains an estimate.',
            'REST snapshots do not reveal every intervening update. No claim of live fill equivalence.'])
    finally:
        if store:store.close()


def register_run(folder,kind,inputs,result):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    identity=hashlib.sha256(encode({'kind':kind,'inputs':inputs,'at':time.time_ns()}).encode()).hexdigest()[:20]
    report={'id':identity,'kind':kind,'created':time.time(),'inputs':inputs,'result':result}
    path=folder/(identity+'.json');path.write_text(encode(report),encoding='utf-8')
    db=sqlite3.connect(str(folder/'experiments.sqlite'))
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created REAL, kind TEXT, inputs TEXT, file TEXT)')
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(identity,time.time(),kind,encode(inputs),path.name))
    db.close();return path


def compare_candles(cfg,rows,source_interval,folder,dataset,cancel=None):
    """Predeclared ablations and cost/path stresses; no automatic winner selection."""
    base=replace(cfg,mode='paper',strategy='ema_rebound',risk_model='fixed',regime_filter=False,entry_method='IOC')
    trials=[('A referanse',base,{}),('B kostnadsfilter',replace(base,min_reward_risk=1.),{}),
        ('C kostnad + ATR og markedsfilter',replace(base,min_reward_risk=1.,risk_model='atr',regime_filter=True),{}),
        ('A alternativ prisrekkefølge',base,{'path':'high-first'}),
        ('A doble kostnader',replace(base,fee=min(.02,base.fee*2),slippage=min(.005,base.slippage*2)),{}),
        ('A forsinket signal',base,{'delay_bars':1}),
        ('A større spread',base,{'spread':.001}),('A mindre tilgjengelig volum',base,{'participation':.025})]
    results=[]
    for name,config,options in trials:
        try:result=candle_replay(config,rows,source_interval,cancel=cancel,**options)
        except InterruptedError:raise
        except Exception as exc:result={'error':type(exc).__name__+': '+str(exc)}
        path=register_run(folder,'candle-replay',{'name':name,'dataset':dataset,'config':asdict(config),'scenario':options},result)
        results.append({'name':name,'file':path.name,'result':result})
    output=Path(folder)/('sammenligning-'+str(time.time_ns())+'.html')
    lines=[]
    for r in results:
        v=r['result'];lines.append('<tr><td>'+html.escape(r['name'])+'</td><td>'+(
            html.escape(v['error']) if 'error' in v else f"€{v['net_eur']:+.2f}</td><td>{v['max_drawdown']:.2%}</td><td>{v['completed_positions']}</td><td>€{v['fees_eur']:.2f}")+'</td></tr>')
    output.write_text('<!doctype html><html lang="nb"><meta charset="utf-8"><title>NordBot sammenligning</title>'
        '<style>body{max-width:1000px;margin:3em auto;font:17px system-ui}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.6em}</style>'
        '<h1>Historikktest med eksplisitte antakelser</h1><p>Samme ordremotor som papir/live. Historisk ordrebok er ukjent. '
        'Ingen av disse testene dokumenterer fremtidig lønnsomhet. Alle forsøk, også feil, er lagret som JSON. '
        'Nyhetsmodell inngår bare i egne kronologiske signaltester.</p><table><tr><th>Forsøk</th><th>Netto EUR</th><th>Største verdifall</th><th>Avsluttede posisjoner</th><th>Gebyr EUR</th></tr>'
        +''.join(lines)+'</table><p>Se JSON-filene for kontant- og kjøp-og-hold-referanse, åpne posisjoner, datasett-ID, konfigurasjon, forløp og usikkerhet.</p></html>',encoding='utf-8')
    return output,results
