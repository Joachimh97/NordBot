"""One trading-state writer, independent public readers and optional research.

No GUI or RSS operation is performed in the risk path. Public/paper components
never receive user credentials. Private execution events request REST recovery;
only cumulative, reconciled order fills change the financial ledger.
"""
import queue
import sqlite3
import threading
import time
from pathlib import Path
from core import Kraken,Store,SafetyError,validate_market
from execution import ProtectedEngine,preflight,transient
from marketdata import MarketHub,PublicData,Recorder,ExecutionsStream
from simulator import PaperExchange
from persistence import encode
import halts


class Runtime:
    def __init__(self,root,output,stop,sell,wake,emergency,*,api_factory=Kraken,feed_factory=PublicData):
        self.root=Path(root);self.output=output;self.stop=stop;self.sell=sell;self.wake=wake;self.emergency=emergency
        self.api_factory=api_factory;self.feed_factory=feed_factory
    def emit(self,kind,data):self.output.put((kind,data))
    def wait(self,seconds):self.wake.wait(seconds);self.wake.clear()
    async def kill_switch(self):
        """Nonblocking request; the sole state writer reconciles and sells bot-owned qty."""
        self.emergency.set();self.wake.set()
        return {'requested':True,'sale_confirmed':False}
    def run(self,c,key='',secret='',reconcile=False,research_mode='Av',session_path=None,use_websocket=True,record=True):
        store=engine=feed=recorder=executions=poller=research=None;failures=0;last_error='';last_notice=0.
        hub=MarketHub(c.pair,c.interval);next_service=next_export=0.;first_new_bar={};private_error_seen=''
        api=None
        try:
            c.validate()
            if c.mode=='live':
                research_mode='Av';api=self.api_factory(key,secret)
                if reconcile=='check':
                    r=preflight(c,api)
                    self.emit('log',f"Tilkobling kontrollert uten ordre. EUR: {r['eur']:.2f}; krypto: {r['base']:.8f}. "
                        f"Maker {r['maker']:.2%}, taker {r['taker']:.2%}. Minste ordre omtrent EUR {r['min_eur']:.2f}. "
                        f"Åpne ordre: {r['orders']}. Utførelser og stop-loss er ikke testet.")
                    return
            else:
                # Discard credentials before constructing any paper/public component.
                key=secret=''
                if reconcile=='check':raise SafetyError('Tilkoblingskontroll gjelder live-konto; papir krever ingen nøkkel.')
            path=session_path or self.root/('live.sqlite' if c.mode=='live' else 'paper-v13.sqlite')
            store=Store(path)
            while not self.stop.is_set():
                try:
                    if api and callable(getattr(api,'begin_cycle',None)):api.begin_cycle()
                    if engine is None and c.mode=='live':
                        engine=ProtectedEngine(c,store,api)
                    if engine and (self.sell.is_set() or self.emergency.is_set()):
                        engine.request_exit('Manuell nødstopp' if self.emergency.is_set() else 'Selg posisjon og pause',emergency=self.emergency.is_set())
                    if engine and reconcile=='release':
                        engine.release_manual_halt();self.emit('log','Manuell sperre frigitt når boten er flat. Automatiske sperrer er beholdt.');return
                    if engine and (c.mode=='live' and (reconcile or self.sell.is_set() or self.emergency.is_set())):
                        engine.service();self.emit('protection',engine.protection_status())
                        if reconcile:
                            self.emit('log','Lagrede ordre avstemt. Ingen nye kjøp startet.');return
                        if not engine.s['qty'] and not engine.active():
                            self.emit('flat',{'cash':engine.s['cash'],'capital':c.capital,'fees':engine.s['fees'],'trades':engine.s['trades']})
                            self.emit('log','Botbeholdning og botordre er avsluttet.');break
                        self.wait(2);continue
                    if feed is None:
                        if record:
                            recorder=Recorder(self.root/'opptak'/str(time.time_ns()));recorder.start();hub.recorder=recorder
                        feed=self.feed_factory(hub,use_websocket=use_websocket);feed.start()
                        if c.mode=='live' and use_websocket:
                            executions=ExecutionsStream(api,self.wake,self.stop);executions.start()
                        if c.mode=='paper' and research_mode!='Av':
                            from news_archive import ArchivePoller
                            from learning import ResearchWorker
                            poller=ArchivePoller(self.root/'news-v13.sqlite');poller.start()
                            research=ResearchWorker(self.root/'learning-v13.sqlite',self.root/'news-v13.sqlite',c);research.start()
                    snap=hub.snapshot();now=time.time();snap['at']=now
                    if engine is None:
                        if not snap['meta'] or not snap['quote_ready']:
                            self.emit('health',{'message':'Venter på ferske offentlige priser og markedsregler. Ingen ordre sendes.',**snap})
                            self.wait(2);continue
                        broker=PaperExchange(c,*snap['meta'],store,now)
                        broker.update(snap['book'],now,rows=snap['rows'],trades=snap['trades'],last=snap['last'])
                        engine=ProtectedEngine(c,store,broker)
                    def entry_guard():
                        current=hub.snapshot()
                        return not (self.stop.is_set() or self.sell.is_set() or self.emergency.is_set()) and current['quote_ready'] and current['status']=='online' and not current['errors'].get('system')
                    engine.entry_guard=entry_guard
                    if not getattr(engine,'announced',False):
                        engine.announced=True
                        self.emit('log',f'{c.mode.upper()} startet med {c.strategy}, {c.interval} min og {c.entry_method}. '
                            f'Maks {c.max_buys} kjøp/døgn, risiko {c.risk:.1%}; grensene er beregnede, ikke garantert tapsbeskyttelse.')
                    if executions:
                        notifications=0
                        while notifications<500:
                            try:event=executions.events.get_nowait()
                            except queue.Empty:break
                            notifications+=1
                            # Persist only execution channel; token/credentials can never reach this table.
                            with store.db:store.db.execute('INSERT INTO diagnostics(ts,kind,value) VALUES (?,?,?)',
                                (now,'private_execution',encode(event)))
                            for row in event.get('data',[]):
                                if row.get('ratecount') is not None and hasattr(api,'governor'):api.governor.observe(row['ratecount'])
                        if notifications or executions.error!=private_error_seen:next_service=min(next_service,now)
                        private_error_seen=executions.error
                    if c.mode=='paper' and snap['quote_ready']:
                        if snap['meta']:engine.api.metadata=snap['meta'][1]
                        engine.api.update(snap['book'],now,rows=snap['rows'],trades=snap['trades'],last=snap['last'])
                    closing=self.sell.is_set() or self.emergency.is_set()
                    if closing:engine.request_exit('Manuell nødstopp' if self.emergency.is_set() else 'Selg posisjon og pause',emergency=self.emergency.is_set())
                    can_service=c.mode=='live' or snap['quote_ready']
                    if can_service and (now>=next_service or closing):
                        engine.service();next_service=now+(3 if engine.active('entry') or engine.s['exit_reason'] else 15)
                    if closing and not engine.s['qty'] and not engine.active():
                        self.emit('flat',{'cash':engine.s['cash'],'capital':c.capital,'fees':engine.s['fees'],'trades':engine.s['trades']})
                        self.emit('log','Botbeholdningen er avsluttet. Pauser.');break
                    if reconcile:
                        self.emit('log','Papirordre avstemt. Ingen nye kjøp startet.');return
                    if not snap['quote_ready']:
                        self.emit('health',{'message':'Priser er utdaterte. Nye kjøp er sperret; avstemming fortsetter. Lokal kursstyrt beskyttelse mangler fersk pris.',**snap})
                        self.emit('protection',engine.protection_status());self.wait(2);continue
                    bid,ask=float(snap['book']['bids'][0][0]),float(snap['book']['asks'][0][0])
                    # This control always precedes and is independent of candle/news validation.
                    engine.monitor(bid,ask,now,liquidate=closing)
                    history_ok=True
                    try:validate_market(snap['rows'],bid,ask,now,c.interval)
                    except SafetyError:history_ok=False
                    entry_allowed=history_ok and snap['status']=='online' and not snap['errors'].get('system') and not closing
                    model=None;model_error=''
                    if research:
                        if snap['meta']:research.submit(snap)
                        model,model_error=research.snapshot()
                        if model:self.emit('research',model)
                    bar=snap['rows'][-1][0] if history_ok else None
                    awaiting_model=False
                    if research_mode=='Filtrer papirkjøp':
                        first_new_bar.setdefault(bar,now)
                        awaiting_model=bool(history_ok and (not model or model['bar']!=bar) and now-first_new_bar[bar]<10 and not model_error)
                        entry_allowed=entry_allowed and bool(model and not model_error and model['bar']==bar and now-model['at']<=10 and model['allowed'])
                    if history_ok and not awaiting_model and bar>engine.s['last_bar']:
                        result=engine.tick(snap['rows'],bid,ask,now,entry_allowed=entry_allowed)
                    else:
                        reason=('Venter kort på fryst papirfilter; salgsregler kjører' if awaiting_model else
                                'Strategihistorikk mangler. Posisjon overvåkes med ferske priser.' if not history_ok else '')
                        result=engine.snapshot(bid,ask,reason,snap['rows'])
                    result.update(data_source=snap['source'],quote_age=snap['age'],history_ok=history_ok,
                        capture_dropped=snap['capture_dropped'],model_error=model_error)
                    self.emit('tick',result)
                    if api and (now>=next_export or engine.s['last_trade']>engine.s.get('fill_details_attempted',0)):
                        engine.collect_fill_details();next_export=now+60
                        engine.s['fill_details_attempted']=now
                        store.save(engine.s)
                    from telemetry import observe as observe_execution
                    observe_execution(store.db,bid,ask,now)
                    self.emit('health',{'message':'Overvåker priser og ordre',**snap,
                        'private_stream':executions.ready if executions else False,'private_stream_error':executions.error if executions else '',
                        'api_budget':api.governor.status() if api and hasattr(api,'governor') else {},
                        'fill_export_error':engine.s.get('fill_export_error',''),
                        'model_error':model_error,'news_error':poller.error if poller else ''})
                    failures=0;last_error=''
                    self.wait(2)
                except Exception as exc:
                    if self.stop.is_set():break
                    next_service=0.  # A failed entry/stop must be reconciled on the very next pass.
                    failures+=1;delay=min(30,2**min(failures,5));msg=f'{type(exc).__name__}: {exc}'
                    if msg!=last_error or time.time()-last_notice>=60:
                        for value in (key,secret):
                            if value:msg=msg.replace(value,'[skjult]')
                        self.emit('log',f'{msg}. Nye kjøp sperres; neste kontroll om {delay} s. Ukjente ordre sendes ikke på nytt.')
                        last_error=msg;last_notice=time.time()
                        if engine:self.emit('protection',engine.protection_status()+' • Avstemming uavklart')
                    if not transient(exc):
                        if isinstance(exc,sqlite3.Error):raise
                        if engine is None or not (engine.s['qty'] or engine.active()):raise
                        # Keep reconciling an existing position; no eight-attempt abandonment.
                        if not isinstance(exc,SafetyError):raise
                    self.wait(delay)
                finally:
                    if api and callable(getattr(api,'end_cycle',None)):api.end_cycle()
        except Exception as exc:
            msg=f'{type(exc).__name__}: {exc}'
            for value in (key,secret):
                if value:msg=msg.replace(value,'[skjult]')
            self.emit('error','Arbeideren stoppet: '+msg+'\nKontroller eventuell botbeholdning og beskyttelsesordre på Kraken.')
        finally:
            if executions:executions.close()
            if feed:feed.close()
            if research:research.close()
            if poller:poller.close()
            if recorder:recorder.close()
            if store:store.close()
            self.emit('done',None)
