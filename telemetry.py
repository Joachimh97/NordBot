"""Execution quality diagnostics. No orders and no portfolio accounting here."""
import csv
import json
import time
from decimal import Decimal


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS execution_quality(trade_id TEXT PRIMARY KEY,
        fill_time REAL,side TEXT,qty TEXT,price TEXT,fee TEXT,decision_reference TEXT,
        adverse_slippage_bps REAL,decision_to_fill_s REAL,maker TEXT,marks TEXT,complete INTEGER)''')


def record(db,identity,row,record):
    initialize(db);quote=record.get('decision_quote',{})
    reference=quote.get('ask' if row['type']=='buy' else 'bid')
    price=Decimal(str(row['price']));slip=None
    if reference:
        slip=float((price/Decimal(str(reference))-1)*10000)*(1 if row['type']=='buy' else -1)
    with db:db.execute('INSERT OR IGNORE INTO execution_quality VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (identity,float(row['time']),row['type'],str(row['vol']),str(row['price']),str(row['fee']),str(reference) if reference else None,
         slip,max(0,float(row['time'])-record['at']),str(row.get('maker','unknown')),'{}',0))


def observe(db,bid,ask,now):
    initialize(db);mid=(bid+ask)/2
    rows=db.execute('SELECT trade_id,fill_time,side,price,marks FROM execution_quality WHERE complete=0').fetchall()
    with db:
        for identity,filled,side,price,raw in rows:
            marks=json.loads(raw)
            for seconds in (5,30,60):
                key=str(seconds);due=filled+seconds
                if key in marks or now<due:continue
                marks[key]={'status':'missed'} if now-due>10 else {'status':'observed','at':now,'mid':mid,
                    'favorable_bps':(mid/float(price)-1)*10000*(1 if side=='buy' else -1)}
            db.execute('UPDATE execution_quality SET marks=?,complete=? WHERE trade_id=?',(json.dumps(marks),int(len(marks)==3),identity))


def export(db,destination):
    initialize(db)
    columns=[r[1] for r in db.execute('PRAGMA table_info(execution_quality)')]
    with open(destination,'w',encoding='utf-8-sig',newline='') as file:
        writer=csv.writer(file);writer.writerow(columns)
        writer.writerows(db.execute('SELECT * FROM execution_quality ORDER BY fill_time,trade_id'))


def execution_summary(state):
    entries=[r for r in state.get('orders_v2',{}).values() if r['role']=='entry']
    requested=sum(float(r['payload']['volume']) for r in entries)
    filled=sum(float(r['seen'][0]) for r in entries)
    acks=[r['ack_seconds'] for r in state.get('orders_v2',{}).values() if 'ack_seconds' in r]
    return {'entry_intents':len(entries),'unfilled_entries':sum(float(r['seen'][0])==0 for r in entries),
        'partial_entries':sum(0<float(r['seen'][0])<float(r['payload']['volume']) for r in entries),
        'quantity_fill_rate':filled/requested if requested else None,
        'mean_ack_seconds':sum(acks)/len(acks) if acks else None,
        'max_protection_confirmation_seconds':state.get('max_protection_confirmation_seconds',0),
        'max_exit_unprotected_seconds':state.get('max_exit_unprotected_seconds',0)}
