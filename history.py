"""Streaming candle import with immutable dataset provenance. Never fills gaps.

Kraken's downloadable OHLCVT CSV has 7 columns (REST OHLC has 8).
Files are supplied locally; no large downloads or trading credentials are needed.
"""
import csv
import hashlib
import io
import json
import math
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from core import SafetyError


def timestamp(value):
    try:number=float(value)
    except ValueError:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:raise ValueError('Dato må ha tidssone.')
        number=dt.timestamp()
    if not math.isfinite(number) or number<=0 or int(number)!=number:
        raise ValueError('Tidspunkt må være hele Unix-sekunder eller dato med tidssone.')
    return int(number)


def candle(row):
    if len(row) not in (7,8):raise ValueError('Forventet timestamp,open,high,low,close,volume,trades (7 kolonner).')
    t=timestamp(row[0]);o,h,l,c=map(float,row[1:5])
    volume=float(row[5] if len(row)==7 else row[6]);trades=int(row[-1])
    if not all(math.isfinite(v) for v in (o,h,l,c,volume)) or min(o,h,l,c)<=0 or volume<0 or trades<0:
        raise ValueError('Ugyldig pris, volum eller antall handler.')
    if h<max(o,c,l) or l>min(o,c,h):raise ValueError('OHLC-prisene er innbyrdes inkonsistente.')
    return [t,str(o),str(h),str(l),str(c),'0',str(volume),trades]


class History:
    def __init__(self,path):
        self.path=Path(path);self.db=sqlite3.connect(str(path))
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candles(dataset TEXT, ts INTEGER, row TEXT NOT NULL, PRIMARY KEY(dataset,ts));''')
    def close(self):self.db.close()
    def datasets(self):return [json.loads(r[0]) for r in self.db.execute('SELECT metadata FROM datasets')]
    def info(self,identity):
        r=self.db.execute('SELECT metadata FROM datasets WHERE id=?',(identity,)).fetchone()
        if not r:raise ValueError('Datasettet finnes ikke.')
        return json.loads(r[0])
    def rows(self,identity,start=0,end=2**62):
        for row in self.db.execute('SELECT row FROM candles WHERE dataset=? AND ts>=? AND ts<? ORDER BY ts',(identity,start,end)):
            yield json.loads(row[0])
    def import_file(self,file,pair,interval=1,member=None):
        if pair not in ('XBTEUR','ETHEUR') or type(interval) is not int or interval not in (1,5,15):
            raise ValueError('Velg BTC/EUR eller ETH/EUR og 1, 5 eller 15 minutter.')
        path=Path(file);digest=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
        identity=hashlib.sha256(f'{digest.hexdigest()}|{member}|{pair}|{interval}'.encode()).hexdigest()
        if self.db.execute('SELECT 1 FROM datasets WHERE id=?',(identity,)).fetchone():return self.info(identity)
        archive=None
        try:
            if path.suffix.lower()=='.zip':
                archive=zipfile.ZipFile(path)
                names=[n for n in archive.namelist() if n.lower().endswith('.csv')]
                if not member:
                    if len(names)!=1:raise ValueError('ZIP inneholder flere CSV-filer. Pakk ut ønsket EUR-fil først, eller velg --member.')
                    member=names[0]
                info=archive.getinfo(member)
                if info.file_size>2*1024**3 or info.file_size/max(1,info.compress_size)>300:
                    raise ValueError('CSV er for stor eller har uvanlig kompresjon.')
                raw=archive.open(member)
            else:raw=path.open('rb')
            count=duplicates=gaps=0;first=last=None
            with raw,io.TextIOWrapper(raw,encoding='utf-8-sig',newline='') as f,self.db:
                for line,row in enumerate(csv.reader(f),1):
                    if not row:continue
                    if line==1 and row[0].strip().lower() in ('timestamp','time','date','unix'):continue
                    try:values=candle(row)
                    except Exception as exc:raise ValueError(f'CSV-linje {line}: {exc}') from exc
                    t=values[0]
                    if t%(interval*60):raise ValueError(f'CSV-linje {line}: tidspunkt er ikke på intervallgrensen.')
                    if last is not None and t<last:raise ValueError('CSV må være kronologisk sortert.')
                    encoded=json.dumps(values,separators=(',',':'))
                    old=self.db.execute('SELECT row FROM candles WHERE dataset=? AND ts=?',(identity,t)).fetchone()
                    if old:
                        if old[0]!=encoded:raise ValueError('Samme tidspunkt har ulike priser. Velg en entydig datakilde.')
                        duplicates+=1;continue
                    if last is not None and t-last>interval*60:gaps+=(t-last)//(interval*60)-1
                    self.db.execute('INSERT INTO candles VALUES (?,?,?)',(identity,t,encoded))
                    count+=1;first=t if first is None else first;last=t
                if count<61:raise ValueError('Trenger minst 61 prisperioder.')
                metadata={'id':identity,'pair':pair,'interval':interval,'file':path.name,'member':member,
                    'sha256':digest.hexdigest(),'imported_at':time.time(),'count':count,'first':first,'last':last,
                    'duplicates':duplicates,'missing_intervals':gaps,'filled_gaps':False,
                    'pair_verified':'user_selected; CSV itself may not identify its market'}
                self.db.execute('INSERT INTO datasets VALUES (?,?)',(identity,json.dumps(metadata)))
            return metadata
        finally:
            if archive:archive.close()


def aggregate(rows,source_interval,target_interval):
    """Only complete aligned groups. Missing sub-bars produce a missing parent bar."""
    if target_interval<source_interval or target_interval%source_interval:raise ValueError('Ugyldig aggregering.')
    needed=target_interval//source_interval;group=[];bucket=None
    def finish(items,start):
        if len(items)!=needed or items[0][0]!=start or any(b[0]-a[0]!=source_interval*60 for a,b in zip(items,items[1:])):
            return None
        return [start,items[0][1],str(max(float(r[2]) for r in items)),str(min(float(r[3]) for r in items)),
                items[-1][4],'0',str(sum(float(r[6]) for r in items)),sum(int(r[7]) for r in items)]
    for row in rows:
        start=int(row[0])//(target_interval*60)*(target_interval*60)
        if bucket is not None and start!=bucket:
            result=finish(group,bucket)
            if result:yield result
            group=[]
        bucket=start;group.append(row)
    if group:
        result=finish(group,bucket)
        if result:yield result
