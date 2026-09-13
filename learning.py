"""Versioned, forward-only signal research. Candidates cannot control live orders.

Each eligible signal gets an independent simulated position with the same order
and exit engine. Overlapping trials are counterfactuals, NOT a portfolio return.
Paper filtering uses a manually selected frozen version. Learning never changes
risk settings, exits, API permissions, the current version or live mode.
"""
import csv
import hashlib
import json
import math
import queue
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime,timezone
from core import SafetyError,Config,validate_market
from policy import decision,model_context
from research import features,probability
from news_archive import NewsArchive
from evaluation import make_engine,reliability,purged_splits,block_bootstrap,register_run
from persistence import encode


def update(weights,x,y,rate=.03):
    p=probability(weights,x)
    return [max(-10,min(10,w+rate*((y-p)*v-(.001*w if i else 0)))) for i,(w,v) in enumerate(zip(weights,x))]


def price_features(x):return x[:6]+[0.,0.,0.]


def fit(rows,prices_only=False):
    weights=[0.]*9
    for r in sorted(rows,key=lambda v:(v['closed'],v['id'])):
        weights=update(weights,price_features(r['x']) if prices_only else r['x'],r['y'])
    return weights


class SignalLab:
    def __init__(self,path,cfg):
        if cfg.mode!='paper':raise SafetyError('Læringslaboratoriet godtar bare papirhandel.')
        self.cfg=cfg;self.path=path;self.db=sqlite3.connect(str(path));self.pending={}
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS candidates(context TEXT PRIMARY KEY,state TEXT);
        CREATE TABLE IF NOT EXISTS trials(id INTEGER PRIMARY KEY,context TEXT,bar INTEGER,created REAL,
            closed REAL,status TEXT,x TEXT,p REAL,price_p REAL,baseline REAL,y INTEGER,net REAL,
            news TEXT,simulation TEXT,frozen_id TEXT,frozen_p REAL,UNIQUE(context,bar));
        CREATE INDEX IF NOT EXISTS trial_context ON trials(context,status,created);
        CREATE TABLE IF NOT EXISTS model_versions(id TEXT PRIMARY KEY,context TEXT,created REAL,
            trained_until REAL,kind TEXT,weights TEXT,training_n INTEGER,evaluation TEXT,role TEXT);
        CREATE TABLE IF NOT EXISTS lab_events(id INTEGER PRIMARY KEY,at REAL,kind TEXT,detail TEXT);''')
        self.context=hashlib.sha256(encode(model_context(cfg)).encode()).hexdigest()
        row=self.db.execute('SELECT state FROM candidates WHERE context=?',(self.context,)).fetchone()
        self.s=json.loads(row[0]) if row else {'weights':[0.]*9,'price_weights':[0.]*9,'n':0,'wins':0,'last_closed':0}
    def close(self):
        for _,_,st in self.pending.values():st.close()
        self.db.close()
    def outcomes(self,status='scored'):
        names=['id','created','closed','status','x','p','price_p','baseline','y','net','frozen_id','frozen_p']
        rows=[]
        for values in self.db.execute('SELECT '+','.join(names)+' FROM trials WHERE context=? AND status=? ORDER BY created,id',(self.context,status)):
            r=dict(zip(names,values));r['x']=json.loads(r['x']);rows.append(r)
        return rows
    def frozen(self):
        row=self.db.execute("SELECT id,weights,kind,created,trained_until FROM model_versions WHERE context=? AND role='paper_champion' ORDER BY created DESC LIMIT 1",(self.context,)).fetchone()
        return {'id':row[0],'weights':json.loads(row[1]),'kind':row[2],'created':row[3],'trained_until':row[4]} if row else None
    def freeze(self,kind='news'):
        if kind not in ('news','prices'):raise ValueError('Velg news eller prices.')
        weights=self.s['weights' if kind=='news' else 'price_weights'];now=time.time()
        identity=hashlib.sha256(encode([self.context,weights,now,kind]).encode()).hexdigest()[:24]
        with self.db:self.db.execute('INSERT INTO model_versions VALUES (?,?,?,?,?,?,?,?,?)',
            (identity,self.context,now,self.s['last_closed'],kind,encode(weights),self.s['n'],'{}','candidate'))
        return identity
    def select_paper(self,identity):
        row=self.db.execute('SELECT context FROM model_versions WHERE id=?',(identity,)).fetchone()
        if not row or row[0]!=self.context:raise ValueError('Modellen tilhører andre innstillinger eller finnes ikke.')
        with self.db:
            self.db.execute("UPDATE model_versions SET role='retired' WHERE context=? AND role='paper_champion'",(self.context,))
            self.db.execute("UPDATE model_versions SET role='paper_champion' WHERE id=?",(identity,))
            self.db.execute('INSERT INTO lab_events(at,kind,detail) VALUES (?,?,?)',(time.time(),'manual_paper_selection',identity))
    def versions(self):
        return [dict(zip(('id','created','kind','training_n','role'),r)) for r in self.db.execute(
            'SELECT id,created,kind,training_n,role FROM model_versions WHERE context=? ORDER BY created DESC',(self.context,))]
    def _persist_trial(self,ident,engine,broker,last):
        snapshot={'engine':engine.s,'exchange':broker.state,'last':last}
        self.db.execute('UPDATE trials SET simulation=? WHERE id=?',(encode(snapshot),ident))
    def _restore(self,ident,snapshot,meta,now):
        engine,broker,st=make_engine(self.cfg,snapshot['exchange']['observed_at'],meta)
        broker.state=snapshot['exchange'];broker.now=snapshot['exchange']['observed_at']
        # Reconstruct through the normal migration and reconciliation path.
        st.state=snapshot['engine']
        from execution import ProtectedEngine
        engine=ProtectedEngine(self.cfg,st,broker)
        self.pending[ident]=(engine,broker,st)
        return engine,broker,st
    def observe(self,snapshot,news):
        now=snapshot['at'];rows=snapshot['rows'];book=snapshot['book'];meta=snapshot['meta'][1]
        bid,ask=float(book['bids'][0][0]),float(book['asks'][0][0])
        if not snapshot['quote_ready']:return self.summary(news,'Manglende fersk pris')
        valid=True
        try:validate_market(rows,bid,ask,now,self.cfg.interval)
        except SafetyError:valid=False
        # Replay each independent pending signal using only newly observed market data.
        pending=self.db.execute("SELECT id,x,p,price_p,baseline,simulation FROM trials WHERE context=? AND status='pending' ORDER BY id",(self.context,)).fetchall()
        for ident,x,p,pp,baseline,raw in pending:
            old=json.loads(raw)
            if now-old['last']>20 or now<old['last'] or not valid:
                with self.db:self.db.execute("UPDATE trials SET status='missed',closed=?,simulation=NULL WHERE id=?",(now,ident))
                if ident in self.pending:self.pending.pop(ident)[2].close()
                continue  # No invented stop/target outcomes across a missing observation window.
            engine,broker,st=self.pending.get(ident) or self._restore(ident,old,meta,now)
            broker.metadata=meta;broker.update(book,now,rows,trades=snapshot['trades'],last=snapshot['last'])
            engine.service();engine.monitor(bid,ask,now)
            if valid:engine.tick(rows,bid,ask,now,entry_allowed=False)
            with self.db:
                if not engine.s['qty'] and not engine.active():
                    if not engine.s['trades']:
                        self.db.execute("UPDATE trials SET status='not_filled',closed=?,simulation=NULL WHERE id=?",(now,ident))
                        self.pending.pop(ident,None);st.close();continue
                    net=engine.s['cash']-self.cfg.capital;y=int(net>0);x=json.loads(x)
                    self.s['weights']=update(self.s['weights'],x,y)
                    self.s['price_weights']=update(self.s['price_weights'],price_features(x),y)
                    self.s['n']+=1;self.s['wins']+=y;self.s['last_closed']=now
                    self.db.execute("UPDATE trials SET status='scored',closed=?,y=?,net=?,simulation=NULL WHERE id=?",(now,y,net,ident))
                    self.pending.pop(ident,None);st.close()
                else:self._persist_trial(ident,engine,broker,now)
                self.db.execute('INSERT OR REPLACE INTO candidates VALUES (?,?)',(self.context,encode(self.s)))
        if not valid:return self.summary(news,'Strategihistorikken har mangler; utfall utsettes ikke kunstig')
        bar=int(rows[-1][0]);buy,_,_=decision(rows,self.cfg)
        existing=self.db.execute('SELECT 1 FROM trials WHERE context=? AND bar=?',(self.context,bar)).fetchone()
        if buy and not existing:
            # First prediction precedes the entry and any learning from this outcome.
            x=features(rows,bid,ask,news);p=probability(self.s['weights'],x)
            pp=probability(self.s['price_weights'],price_features(x));base=(self.s['wins']+1)/(self.s['n']+2)
            version=self.frozen();fp=None
            if version and version['created']<=now:
                fp=probability(version['weights'],price_features(x) if version['kind']=='prices' else x)
            engine,broker,st=make_engine(self.cfg,now,meta)
            broker.update(book,now,rows,last=snapshot['last']);engine.tick(rows,bid,ask,now)
            accepted=bool(engine.s['qty'] or engine.active())
            with self.db:
                cur=self.db.execute('INSERT INTO trials(context,bar,created,closed,status,x,p,price_p,baseline,news,frozen_id,frozen_p) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.context,bar,now,None if accepted else now,'pending' if accepted else 'not_filled',encode(x),p,pp,base,encode(news),version['id'] if version else None,fp))
                ident=cur.lastrowid
                if accepted:self._persist_trial(ident,engine,broker,now);self.pending[ident]=(engine,broker,st)
                else:st.close()
        return self.summary(news)
    def summary(self,news,reason=''):
        rows=self.outcomes();recent=rows[-200:];model=reliability(recent);reference=reliability(recent,'baseline')
        counts=dict(self.db.execute('SELECT status,count(*) FROM trials WHERE context=? GROUP BY status',(self.context,)))
        latest=self.db.execute('SELECT bar,p,price_p,frozen_id,frozen_p,created FROM trials WHERE context=? ORDER BY created DESC LIMIT 1',(self.context,)).fetchone()
        drift=False
        if len(recent)>=100:
            older=reliability(recent[:-50])['brier'];newer=reliability(recent[-50:])['brier']
            drift=newer>older+.05 or newer>reliability(recent[-50:],'baseline')['brier']+.02
        selected=self.frozen();allowed=False
        if not reason:
            if not selected:reason='Ingen fryst modell valgt for papirfilter'
            elif not latest or latest[3]!=selected['id']:reason='Venter på et nytt signal vurdert av valgt modell'
            elif selected['kind']=='news' and not news['ready']:reason='Nyhetsfilter mangler ferske nyheter'
            elif drift:reason='Driftvarsel: papirfilter stanser nye kjøp'
            elif latest[4] is not None and latest[4]>=.60:allowed=True;reason='Fryst papirfilter godkjenner signalet'
            else:reason='Fryst papirfilter avviser signalet'
        return {'n':len(rows),'counts':counts,'score':latest[1] if latest else .5,'price_score':latest[2] if latest else .5,
            'bar':latest[0] if latest else None,'created':latest[5] if latest else 0,'at':news['at'],
            'brier':model['brier'],'baseline':reference['brier'],'positive_rate':model.get('positive_rate'),
            'calibration':model['bins'],'drift':drift,'allowed':allowed,'reason':reason,'news':news,
            'frozen_id':selected['id'] if selected else None,'context':self.context,
            'target':'Nettoresultat for et strategisignal under samme ordre- og salgsregler; uavhengig papirposisjon'}
    def export(self,path):
        with open(path,'w',newline='',encoding='utf-8-sig') as file:
            names=['id','created','closed','status','x','p','price_p','baseline','y','net','frozen_id','frozen_p']
            writer=csv.DictWriter(file,fieldnames=names);writer.writeheader()
            for state in ('scored','pending','missed','not_filled'):
                for row in self.outcomes(state):writer.writerow(row)
    def evaluate(self,folder):
        rows=self.outcomes();splits,holdout=purged_splits(rows)
        result={'n':len(rows),'folds':[],'holdout_n':len(holdout),'holdout':'Not scored; reserve for one declared final comparison',
            'note':'Signal PnL sums may overlap. Portfolio claims require event replay with the frozen filter.',
            'calibration_prequential':reliability(rows),'class_balance':sum(r['y'] for r in rows)/len(rows) if rows else None}
        for train,validation in splits:
            fold={'train_n':len(train),'validation_n':len(validation),'first_validation':validation[0]['created'],'models':{}}
            for name,prices_only in [('prices',True),('news',False)]:
                weights=fit(train,prices_only);predictions=[];differences=[]
                for r in validation:
                    p=probability(weights,price_features(r['x']) if prices_only else r['x'])
                    predictions.append({**r,'p':p});differences.append((r['net'] if p>=.60 else 0)-r['net'])
                fold['models'][name]={'calibration':reliability(predictions),'unfiltered_signal_net':sum(r['net'] for r in validation),
                    'filtered_signal_net':sum(r['net'] for r in predictions if r['p']>=.60),
                    'signal_difference_bootstrap':block_bootstrap(differences),
                    'weights':weights,'threshold':.60,'automatic_selection':False}
            result['folds'].append(fold)
        return register_run(folder,'purged-walk-forward',{'context':self.context,'embargo_seconds':3600,'final_holdout_fraction':.2},result)
    def evaluate_frozen(self,identity,folder):
        model=self.db.execute('SELECT created,trained_until,kind,weights,context FROM model_versions WHERE id=?',(identity,)).fetchone()
        if not model or model[4]!=self.context:raise ValueError('Ukjent modell i denne konfigurasjonen.')
        created,trained_until,kind,raw,_=model
        rows=[r for r in self.outcomes() if r['created']>max(created,trained_until)+3600]
        weights=json.loads(raw)
        scored=[{**r,'p':probability(weights,price_features(r['x']) if kind=='prices' else r['x'])} for r in rows]
        result={'model_id':identity,'created':created,'training_cutoff':trained_until,'future_outcomes':len(rows),
            'calibration':reliability(scored),'signal_net_filtered':sum(r['net'] for r in scored if r['p']>=.60),
            'signal_net_unfiltered':sum(r['net'] for r in rows),'not_portfolio_pnl':True,'live_qualified':False}
        path=register_run(folder,'frozen-forward-evaluation',{'model_id':identity,'context':self.context},result)
        with self.db:self.db.execute('UPDATE model_versions SET evaluation=? WHERE id=?',(encode(result),identity))
        return path

    def final_holdout(self,kind,folder):
        """Consume one declared final split per context; it cannot be rerun to pick a winner."""
        if kind not in ('news','prices'):raise ValueError('Velg news eller prices før slutt-testen åpnes.')
        marker='final_holdout:'+self.context
        if self.db.execute('SELECT 1 FROM lab_events WHERE kind=?',(marker,)).fetchone():
            raise ValueError('Slutt-testen for disse innstillingene er allerede brukt. Bruk nye fremtidige data, ikke gjentatt modellvalg på samme slutt-test.')
        rows=self.outcomes();split=int(len(rows)*.8)
        if split<100 or len(rows)-split<40:raise ValueError('For få utfall til denne slutt-testen. Minst 200 kreves som teknisk minimum, ikke bevis på kvalitet.')
        holdout=rows[split:];boundary=holdout[0]['created']-3600
        train=[r for r in rows[:split] if r['closed']<boundary]
        weights=fit(train,kind=='prices')
        manifest={'context':self.context,'kind':kind,'threshold':.60,'training_ids':[r['id'] for r in train],
            'holdout_ids':[r['id'] for r in holdout],'weights':weights,'selected_before_scoring':True}
        # Commit selection and consumption BEFORE seeing results, including a failed report write.
        with self.db:self.db.execute('INSERT INTO lab_events(at,kind,detail) VALUES (?,?,?)',(time.time(),marker,encode(manifest)))
        predictions=[{**r,'p':probability(weights,price_features(r['x']) if kind=='prices' else r['x'])} for r in holdout]
        delta=[(r['net'] if r['p']>=.60 else 0)-r['net'] for r in predictions]
        result={'calibration':reliability(predictions),'signal_difference':block_bootstrap(delta),
            'signal_net_filtered':sum(r['net'] for r in predictions if r['p']>=.60),'signal_net_unfiltered':sum(r['net'] for r in holdout),
            'not_portfolio_pnl':True,'live_qualified':False,'requires_future_forward_evaluation':True}
        return register_run(folder,'declared-final-holdout',manifest,result)


class FrozenFilter:
    """Replay a frozen veto on data that arrived after that version was created."""
    def __init__(self,lab_path,news_path,cfg,identity):
        self.lab=SignalLab(lab_path,cfg);self.news=NewsArchive(news_path)
        row=self.lab.db.execute('SELECT context,created,trained_until,kind,weights FROM model_versions WHERE id=?',(identity,)).fetchone()
        if not row or row[0]!=self.lab.context:
            self.close();raise ValueError('Fryst modell samsvarer ikke med konfigurasjonen.')
        self.created=max(row[1],row[2]);self.kind=row[3];self.weights=json.loads(row[4]);self.cfg=cfg
    def close(self):self.lab.close();self.news.close()
    def __call__(self,rows,bid,ask,at):
        if at<=self.created:raise ValueError('Modellen eksisterte ikke før denne opptaksperioden. Dette ville lekke fremtidig informasjon.')
        snapshot=self.news.snapshot(self.cfg.pair,at)
        if self.kind=='news' and not snapshot['ready']:return False
        x=features(rows,bid,ask,snapshot)
        if self.kind=='prices':x=price_features(x)
        return probability(self.weights,x)>=.60


class ResearchWorker:
    """Bounded latest-snapshot queue; model/disk delays never block the risk writer."""
    def __init__(self,path,news_path,cfg):
        if cfg.mode!='paper':raise SafetyError('Nyhetslæring er foreløpig bare tilgjengelig i papirhandel.')
        self.path=path;self.news_path=news_path;self.cfg=cfg;self.q=queue.Queue(2)
        self.stop=threading.Event();self.lock=threading.Lock();self.result=None;self.error='';self.thread=None;self.dropped=0
    def start(self):
        self.thread=threading.Thread(target=self.run,name='NordBot-model-research',daemon=False);self.thread.start()
    def submit(self,snapshot):
        try:self.q.put_nowait(snapshot)
        except queue.Full:
            self.dropped+=1
            try:self.q.get_nowait()
            except queue.Empty:pass
            try:self.q.put_nowait(snapshot)
            except queue.Full:pass
    def snapshot(self):
        with self.lock:return self.result,self.error
    def run(self):
        lab=news=None
        try:
            lab=SignalLab(self.path,self.cfg);news=NewsArchive(self.news_path)
            while not self.stop.is_set():
                try:snapshot=self.q.get(timeout=.5)
                except queue.Empty:continue
                try:
                    result=lab.observe(snapshot,news.snapshot(self.cfg.pair,snapshot['at']))
                    with self.lock:self.result=result;self.error=''
                except Exception as exc:
                    with self.lock:self.error=type(exc).__name__+': '+str(exc)
        except Exception as exc:
            with self.lock:self.error=type(exc).__name__
        finally:
            if lab:lab.close()
            if news:news.close()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=20)
