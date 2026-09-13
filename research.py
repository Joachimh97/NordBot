"""Legacy 1.2 model retained for regression tests and shared text helpers.
NordBot 1.3 uses learning.py and news_archive.py instead.
Experimental news + online logistic model. No credentials, LLM, orders or code execution.
Prediction target: positive return AFTER configured round-trip costs over next hour.
Only forward-observed outcomes train the model; never backfill news into history.
"""
import csv, hashlib, html, json, math, re, sqlite3, threading, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from core import ema

FEEDS={'CoinDesk':'https://www.coindesk.com/arc/outboundfeeds/rss/',
       'Cointelegraph':'https://cointelegraph.com/rss'}
POS={'approval','approved','adoption','inflows','surge','rally','growth','gains','bullish','upgrade'}
NEG={'hack','hacked','exploit','fraud','bankruptcy','outflows','crash','ban','banned','bearish','lawsuit'}
FEATURES=['bias','return_15m','return_1h','ema_gap','volatility','spread','news_tone','news_count','news_available']

def clean(text):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',text or ''))).strip()

def parse_feed(blob,source,seen):
    if len(blob)>1024*1024 or b'<!DOCTYPE' in blob.upper() or b'<!ENTITY' in blob.upper():
        raise ValueError('Avvist XML-format eller for stor RSS-fil')
    root=ET.fromstring(blob); articles=[]
    items=root.findall('.//item')
    if root.tag not in ('rss','{http://www.w3.org/2005/Atom}feed'):
        raise ValueError('Svaret er ikke en RSS/Atom-feed')
    if not items:items=root.findall('{http://www.w3.org/2005/Atom}entry')
    for item in items[:100]:
        fields={x.tag.split('}')[-1]:x for x in item}
        get=lambda k: ''.join(fields[k].itertext()) if k in fields else ''
        title=clean(get('title'))[:300]
        if not title:continue
        link=get('link').strip()
        if 'link' in fields and fields['link'].get('href'):link=fields['link'].get('href')
        host=(urllib.parse.urlparse(link).hostname or '').lower()
        domain=urllib.parse.urlparse(FEEDS[source]).hostname.removeprefix('www.')
        if urllib.parse.urlparse(link).scheme!='https' or not (host==domain or host.endswith('.'+domain)):continue
        stamp=get('pubDate') or get('published') or get('updated')
        try:
            try:dt=parsedate_to_datetime(stamp)
            except (ValueError,TypeError):dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if dt.tzinfo is None:continue
            published=dt.timestamp()
        except (ValueError,TypeError,OverflowError):continue
        if not seen-86400<=published<=seen+60:continue
        summary=clean(get('description') or get('summary'))[:600]
        words=re.findall(r'[a-z]+',(title+' '+summary).lower())
        # A deliberately simple, fallible tone feature, not fact-checking or reasoning.
        tone=0
        for i,w in enumerate(words):
            value=int(w in POS)-int(w in NEG)
            if any(v in ('no','not','never','without') for v in words[max(0,i-3):i]):value=-value
            tone+=value
        articles.append({'id':hashlib.sha256(title.lower().encode()).hexdigest(),
                         'source':source,'title':title,'summary':summary,'url':link,'published':published,'seen':seen,
                         'tone':max(-1,min(1,tone/3)),
                         'btc':bool(re.search(r'\b(bitcoin|btc|xbt)\b',(title+' '+summary).lower())),
                         'eth':bool(re.search(r'\b(ethereum|ether|eth)\b',(title+' '+summary).lower()))})
    return articles

class NewsPoller:
    def __init__(self):
        self.stop=threading.Event();self.lock=threading.Lock();self.articles={};self.health={}
        self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self.run,name='NordBot-news',daemon=False);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=25)
    def run(self):
        while not self.stop.is_set():
            for name,url in FEEDS.items():
                if self.stop.is_set():break
                try:
                    req=urllib.request.Request(url,headers={'User-Agent':'NordBot/1.1 RSS reader'})
                    with urllib.request.urlopen(req,timeout=8) as response:
                        if urllib.parse.urlparse(response.url).scheme!='https':raise ValueError('Usikker omdirigering')
                        blob=response.read(1024*1024+1)
                    now=time.time();items=parse_feed(blob,name,now)
                    with self.lock:
                        for item in items:
                            if item['id'] not in self.articles:self.articles[item['id']]=item
                        self.health[name]={'ok':now,'error':''}
                except Exception as e:
                    with self.lock:self.health[name]={'ok':self.health.get(name,{}).get('ok',0),'error':type(e).__name__}
            with self.lock:
                self.articles={k:v for k,v in self.articles.items() if v['published']>=time.time()-86400}
            if self.stop.wait(300):break
    def snapshot(self,pair,now):
        with self.lock:
            key='btc' if pair=='XBTEUR' else 'eth'
            items=[dict(v) for v in self.articles.values() if v[key] and v['seen']<=now and now-86400<=v['published']<=now]
            health={k:dict(v) for k,v in self.health.items()}
        healthy=[k for k,v in health.items() if not v['error'] and 0<=now-v['ok']<=900]
        items.sort(key=lambda v:v['published'],reverse=True)
        eligible=[v for v in items if v['source'] in healthy]
        weights=[math.exp(-(now-v['published'])/21600) for v in eligible]
        tone=sum(v['tone']*w for v,w in zip(eligible,weights))/sum(weights) if weights else 0
        return {'tone':tone,'count':len(eligible),'ready':bool(healthy and eligible),
                'articles':items[:5],'health':health,'healthy':len(healthy),'at':now}

def features(rows,bid,ask,news):
    closes=[float(r[4]) for r in rows]
    clip=lambda x:max(-3.,min(3.,x))
    fast,slow=ema(closes,20),ema(closes,50)
    returns=[math.log(b/a) for a,b in zip(closes[-21:-1],closes[-20:])]
    mean=sum(returns)/len(returns)
    vol=math.sqrt(sum((v-mean)**2 for v in returns)/len(returns))
    ready=news['ready']
    return [1.,clip(math.log(closes[-1]/closes[-2])*100),clip(math.log(closes[-1]/closes[-5])*100),
            clip((fast[-1]/slow[-1]-1)*100),clip(vol*100),clip((ask/bid-1)*100),
            float(news['tone']) if ready else 0.,min(news['count'],20)/20 if ready else 0.,float(ready)]

def probability(weights,x):
    z=max(-20,min(20,sum(a*b for a,b in zip(weights,x))))
    return 1/(1+math.exp(-z))

class Learner:
    def __init__(self,path,cfg):
        self.cfg=cfg;self.db=sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS models (key TEXT PRIMARY KEY, state TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY, key TEXT, bar INTEGER, created REAL, due REAL, buy_cost REAL, x TEXT, p REAL, baseline REAL, status TEXT, y INTEGER, realized REAL, news TEXT, UNIQUE(key,bar))')
        self.key=json.dumps(['v1',cfg.pair,cfg.fee,cfg.slippage,FEATURES])
        row=self.db.execute('SELECT state FROM models WHERE key=?',(self.key,)).fetchone()
        self.s=json.loads(row[0]) if row else {'weights':[0.]*len(FEATURES),'n':0,'wins':0,'brier':0.,'baseline_brier':0.,'correct':0,'history':[]}
    def close(self):self.db.close()
    def observe(self,rows,bid,ask,news,now):
        if not all(math.isfinite(v) and v>0 for v in (bid,ask,now)) or bid>ask:raise ValueError('Ugyldige modellpriser')
        x=features(rows,bid,ask,news)
        if not all(math.isfinite(v) for v in x):raise ValueError('Ugyldige modellvariabler')
        s=self.s
        # A single atomic commit for scoring, learning and the next forecast.
        with self.db:
            pending=self.db.execute("SELECT id,due,buy_cost,x,p,baseline FROM predictions WHERE key=? AND status='pending' AND due<=? ORDER BY due",(self.key,now)).fetchall()
            for ident,due,buy,oldx,p,baseline in pending:
                if now-due>120:
                    self.db.execute("UPDATE predictions SET status='missed' WHERE id=?",(ident,));continue
                sell=bid*(1-self.cfg.fee)*(1-self.cfg.slippage)
                realized=sell/buy-1;y=int(realized>0)
                loss=(p-y)**2;base_loss=(baseline-y)**2
                s['n']+=1;s['wins']+=y;s['brier']+=loss;s['baseline_brier']+=base_loss;s['correct']+=int((p>=.5)==bool(y))
                s['history']=(s['history']+[[loss,base_loss]])[-200:]
                oldx=json.loads(oldx)
                current=probability(s['weights'],oldx)
                s['weights']=[max(-10,min(10,w+.03*((y-current)*v-(.001*w if i else 0)))) for i,(w,v) in enumerate(zip(s['weights'],oldx))]
                self.db.execute("UPDATE predictions SET status='scored',y=?,realized=? WHERE id=?",(y,realized,ident))
            bar=int(rows[-1][0])
            exists=self.db.execute('SELECT p,created FROM predictions WHERE key=? AND bar=?',(self.key,bar)).fetchone()
            if not exists:
                p=probability(s['weights'],x);baseline=(s['wins']+1)/(s['n']+2)
                buy=ask*(1+self.cfg.fee)*(1+self.cfg.slippage)
                self.db.execute('INSERT INTO predictions(key,bar,created,due,buy_cost,x,p,baseline,status,news) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (self.key,bar,now,now+3600,buy,json.dumps(x),p,baseline,'pending',json.dumps(news,ensure_ascii=False)))
                created=now
            else:p,created=exists
            self.db.execute('INSERT OR REPLACE INTO models VALUES (?,?)',(self.key,json.dumps(s,allow_nan=False)))
        n=s['n'];hist=s['history'];recent=sum(v[0] for v in hist)/len(hist) if hist else None
        base=sum(v[1] for v in hist)/len(hist) if hist else None
        reason='Filter: venter på minst 200 vurderte prognoser'
        allowed=False
        if not news['ready']:reason='Filter: mangler ferske, relevante nyheter'
        elif now-created>960:reason='Filter: prognosen er utdatert'
        elif n>=200:
            if recent+.005>=base:reason='Filter: modellen slår ikke enkel referanse'
            elif p<.60:reason='Filter: modellscore under 60 %'
            else:allowed=True;reason='Filter: kjøp kan tillates dersom vanlig strategisignal også kommer'
        return {'score':p,'n':n,'accuracy':s['correct']/n if n else None,'brier':recent,'baseline':base,
                'allowed':allowed,'reason':reason,'news':news,'created':created,'target':'nettooppgang neste time'}
    def export(self,path):
        with open(path,'w',newline='',encoding='utf-8-sig') as f:
            w=csv.writer(f);w.writerow(['created_utc','due_utc','score','reference_score','status','actual_positive','net_return','news_snapshot'])
            for created,due,p,base,status,y,r,news in self.db.execute('SELECT created,due,p,baseline,status,y,realized,news FROM predictions WHERE key=? ORDER BY created',(self.key,)):
                w.writerow([datetime.fromtimestamp(created,timezone.utc).isoformat(),datetime.fromtimestamp(due,timezone.utc).isoformat(),p,base,status,y,r,news])
