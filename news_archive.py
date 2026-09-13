"""Point-in-time RSS archive. Retains supplied title/summary, not scraped articles.

Source health and every contributing revision accompany each feature snapshot.
Text is always untrusted data. This module cannot obtain keys or execute orders.
"""
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from research import FEEDS,parse_feed

STOPWORDS=set('the a an and or to of for on in with as is are has have at by from after before amid bitcoin btc xbt ethereum eth'.split())


def terms(item):return set(re.findall(r'[a-z]{3,}',item['title'].lower()))-STOPWORDS


def event_type(text):
    rules=[('security',('hack','exploit','breach')),('regulation',('regulator','sec ','approval','lawsuit','ban ')),
           ('fund_flows',('inflow','outflow','etf')),('protocol',('upgrade','fork','halving')),
           ('macro',('interest rate','inflation','federal reserve'))]
    lower=text.lower()
    return next((name for name,keys in rules if any(k in lower for k in keys)),'unknown')


class NewsArchive:
    def __init__(self,path):
        self.db=sqlite3.connect(str(path))
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS articles(revision TEXT PRIMARY KEY,url TEXT,source TEXT,first_seen REAL,
            published REAL,title TEXT,summary TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS article_time ON articles(first_seen,published);
        CREATE TABLE IF NOT EXISTS source_health(id INTEGER PRIMARY KEY,source TEXT,observed REAL,ok REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS text_features(revision TEXT,model TEXT,created REAL,payload TEXT,PRIMARY KEY(revision,model));''')
    def close(self):self.db.close()
    def ingest(self,items,seen):
        with self.db:
            for original in items:
                item=dict(original)
                identity=hashlib.sha256((item['url']+'\n'+item['title']+'\n'+item.get('summary','')).encode()).hexdigest()
                item.update(revision=identity,seen=seen,event_type=event_type(item['title']+' '+item.get('summary','')),
                            claim_confirmed='unknown',analysis='wordlist-v2')
                self.db.execute('INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?)',
                    (identity,item['url'],item['source'],seen,item['published'],item['title'],item.get('summary',''),json.dumps(item)))
    def health(self,source,seen,error=''):
        old=self.db.execute('SELECT ok FROM source_health WHERE source=? ORDER BY id DESC LIMIT 1',(source,)).fetchone()
        with self.db:self.db.execute('INSERT INTO source_health(source,observed,ok,error) VALUES (?,?,?,?)',
            (source,seen,(old[0] if old else 0) if error else seen,error))
    def snapshot(self,pair,now):
        health={}
        for name in FEEDS:
            row=self.db.execute('SELECT ok,error FROM source_health WHERE source=? AND observed<=? ORDER BY id DESC LIMIT 1',(name,now)).fetchone()
            health[name]={'ok':row[0] if row else 0,'error':row[1] if row else 'Ingen henting ennå'}
        healthy={k for k,v in health.items() if not v['error'] and 0<=now-v['ok']<=900}
        asset='btc' if pair=='XBTEUR' else 'eth'
        # Select the latest *known* revision of each URL. Publication time is never availability time.
        urls={}
        for payload in self.db.execute('SELECT payload FROM articles WHERE first_seen<=? AND published>=? AND published<=? ORDER BY first_seen,revision',
                                       (now,now-86400,now)):
            item=json.loads(payload[0])
            if item[asset]:urls[item['url']]=item
        items=sorted(urls.values(),key=lambda a:(a['seen'],a['revision']))
        clusters=[]
        for item in items:
            tokens=terms(item);match=None
            for group in clusters:
                common=len(tokens&group['terms'])/max(1,len(tokens|group['terms']))
                if common>=.55 and abs(item['published']-group['published'])<=21600:
                    match=group;break
            if match is None:
                match={'id':item['revision'][:16],'terms':tokens,'published':item['published'],'items':[]};clusters.append(match)
            item['cluster']=match['id'];match['items'].append(item)
        weighted=[];contributors=[]
        for group in clusters:
            eligible=[a for a in group['items'] if a['source'] in healthy]
            if not eligible:continue
            tone=sum(a['tone'] for a in eligible)/len(eligible)
            weight=math.exp(-(now-max(a['published'] for a in eligible))/21600)
            weighted.append((tone,weight));contributors.extend(a['revision'] for a in eligible)
        tone=sum(t*w for t,w in weighted)/sum(w for _,w in weighted) if weighted else 0.
        return {'at':now,'tone':tone,'ready':bool(weighted),'count':len(weighted),'article_count':len(contributors),
            'articles':sorted(items,key=lambda a:a['seen'],reverse=True),'contributing_revisions':contributors,
            'health':health,'healthy':len(healthy),'analysis':'wordlist-v2, clustered, first-seen limited',
            'unverified_events':len(weighted)}


class ArchivePoller:
    def __init__(self,path):self.path=path;self.stop=threading.Event();self.thread=None;self.error=''
    def start(self):
        self.thread=threading.Thread(target=self.run,name='NordBot-RSS-archive',daemon=False);self.thread.start()
    def run(self):
        archive=None
        try:
            archive=NewsArchive(self.path)
            while not self.stop.is_set():
                for name,url in FEEDS.items():
                    if self.stop.is_set():break
                    try:
                        req=urllib.request.Request(url,headers={'User-Agent':'NordBot/1.3 RSS research'})
                        with urllib.request.urlopen(req,timeout=8) as response:
                            host=(urllib.parse.urlparse(response.url).hostname or '').removeprefix('www.')
                            expected=urllib.parse.urlparse(url).hostname.removeprefix('www.')
                            if urllib.parse.urlparse(response.url).scheme!='https' or not (host==expected or host.endswith('.'+expected)):
                                raise ValueError('RSS ble omdirigert utenfor kildedomenet.')
                            blob=response.read(1024*1024+1)
                        now=time.time();archive.ingest(parse_feed(blob,name,now),now);archive.health(name,now)
                    except Exception as exc:archive.health(name,time.time(),type(exc).__name__)
                self.stop.wait(300)
        except Exception as exc:self.error=type(exc).__name__
        finally:
            if archive:archive.close()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=20)


def annotate_local_finbert(archive_path,model_folder,limit=200):
    """Optional offline batch research; never called in the trading/risk worker.

    User supplies a trusted local transformers model, e.g. ProsusAI/finbert.
    No download, remote-code loading, API bill, or live decision is allowed here.
    """
    from pathlib import Path
    folder=Path(model_folder).resolve()
    if not folder.is_dir() or not 1<=limit<=2000:raise ValueError('Velg lokal modellmappe og 1–2000 artikler.')
    from transformers import AutoTokenizer,AutoModelForSequenceClassification,pipeline
    tokenizer=AutoTokenizer.from_pretrained(str(folder),local_files_only=True,trust_remote_code=False)
    model=AutoModelForSequenceClassification.from_pretrained(str(folder),local_files_only=True,trust_remote_code=False,use_safetensors=True)
    classify=pipeline('text-classification',model=model,tokenizer=tokenizer,device=-1,top_k=None)
    digest=hashlib.sha256()
    for file in sorted(folder.glob('*')):
        if file.is_file():
            digest.update(file.name.encode())
            with file.open('rb') as f:
                for data in iter(lambda:f.read(1024*1024),b''):digest.update(data)
    identity='local-finbert:'+digest.hexdigest();archive=NewsArchive(archive_path);count=0
    try:
        rows=archive.db.execute('SELECT revision,title,summary FROM articles WHERE NOT EXISTS '
            '(SELECT 1 FROM text_features WHERE text_features.revision=articles.revision AND model=?) ORDER BY first_seen LIMIT ?',
            (identity,limit)).fetchall()
        for revision,title,summary in rows:
            result=classify((title+' '+summary)[:2000],truncation=True,max_length=256)
            if result and isinstance(result[0],list):result=result[0]
            labels={r['label'].lower():float(r['score']) for r in result}
            if set(labels)!={'positive','negative','neutral'} or not all(math.isfinite(v) and 0<=v<=1 for v in labels.values()):
                raise ValueError('Modellen returnerte ikke validerbare FinBERT-etiketter.')
            with archive.db:archive.db.execute('INSERT OR IGNORE INTO text_features VALUES (?,?,?,?)',
                (revision,identity,time.time(),json.dumps(labels)))
            count+=1
        return {'model':identity,'articles':count,'use':'offline annotation only; not enabled in trades'}
    finally:archive.close()
