"""Consistent backups, bounded audit data and durable nonces. No credentials on disk."""
import csv
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      default=lambda v: str(v) if isinstance(v, Decimal) else _unsupported(v))


def _unsupported(value): raise TypeError(type(value).__name__)


class NonceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS nonces (fingerprint TEXT PRIMARY KEY, value INTEGER NOT NULL)')

    def next(self, fingerprint, wall_ms=None):
        wall_ms = time.time_ns()//1000000 if wall_ms is None else int(wall_ms)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM nonces WHERE fingerprint=?', (fingerprint,)).fetchone()
            value = max(wall_ms, row[0]+1 if row else 0)
            if not 0 < value < 2**63: raise ValueError('Nonce utenfor støttet område.')
            db.execute('INSERT OR REPLACE INTO nonces VALUES (?,?)', (fingerprint, value))
        return value


def backup_database(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve(): raise ValueError('Backup må være en egen fil.')
    if not source.exists() or destination.exists(): raise ValueError('Kilde mangler eller målfilen finnes allerede.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer, pages=128)
        if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup besto ikke integritetskontroll.')
    finally:
        writer.close(); reader.close()
    return destination


def restore_database(backup, destination):
    """Offline only: do not overwrite an existing session or discard its WAL."""
    destination = Path(destination)
    if any(Path(str(destination)+suffix).exists() for suffix in ('', '-wal', '-shm')):
        raise ValueError('Gjenoppretting krever et tomt mål. Bevar den gamle journalen og lukk NordBot først.')
    backup_database(backup, destination)
    with sqlite3.connect(destination) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='state'").fetchone():
            row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
            if row:
                state = json.loads(row[0])
                state['restored_at'] = time.time()
                state['reconcile_required'] = True
                db.execute('UPDATE state SET value=? WHERE id=1', (encode(state),))
    return destination


def initialize_tables(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS order_records (client_id TEXT PRIMARY KEY, txid TEXT,
        role TEXT, status TEXT, parent_txid TEXT, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS halt_records (code TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS exchange_fills (trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
        ts REAL NOT NULL, pair TEXT, side TEXT, qty TEXT, price TEXT, cost TEXT, fee TEXT,
        fee_currency TEXT, raw TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS account_observations (id INTEGER PRIMARY KEY, ts REAL,
        value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS diagnostics (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, value TEXT);
    ''')
    db.commit()


def mirror_records(db, state):
    for key, record in state.get('orders_v2', {}).items():
        db.execute('INSERT OR REPLACE INTO order_records VALUES (?,?,?,?,?,?)',
                   (key, record.get('txid'), record['role'], record['status'],
                    record.get('parent_txid'), encode(record)))
    db.execute('DELETE FROM halt_records')
    for code, record in state.get('halts', {}).items():
        db.execute('INSERT INTO halt_records VALUES (?,?)', (code, encode(record)))


def record_exchange_fill(db, trade_id, row, pair, fee_currency):
    """Independent exchange executions; never use them to double-book cumulative fills."""
    data = (str(trade_id), str(row['ordertxid']), float(row['time']), pair, row['type'],
            str(row['vol']), str(row['price']), str(row['cost']), str(row['fee']),
            fee_currency, encode(row))
    old = db.execute('SELECT raw FROM exchange_fills WHERE trade_id=?', (str(trade_id),)).fetchone()
    if old and old[0] != data[-1]:
        db.execute('INSERT INTO diagnostics(ts,kind,value) VALUES (?,?,?)',
                   (time.time(), 'exchange_fill_revision', encode({'id': trade_id, 'previous': json.loads(old[0]), 'new': row})))
    db.execute('INSERT OR REPLACE INTO exchange_fills VALUES (?,?,?,?,?,?,?,?,?,?,?)', data)


def export_fills(db, destination, fx_csv=None):
    """Optional explicitly sourced, non-future EUR/NOK observations; blanks if missing."""
    rates = []
    if fx_csv:
        with open(fx_csv, encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
                if stamp.tzinfo is None: raise ValueError('Valutakurser må ha tidssone.')
                rate = Decimal(row['EUR_NOK'])
                if not rate.is_finite() or rate <= 0 or not row['source'].strip(): raise ValueError('Ugyldig valutakurs.')
                rates.append((stamp.timestamp(), rate, row['source']))
        rates.sort(key=lambda x: x[0])
    missing = 0
    with open(destination, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['trade_id', 'order_id', 'UTC', 'pair', 'side', 'qty', 'price_EUR',
                    'cost_EUR', 'fee', 'fee_currency', 'EUR_NOK', 'FX_time_UTC', 'FX_source',
                    'cost_NOK', 'fee_NOK', 'FX_status'])
        for row in db.execute('SELECT trade_id,order_id,ts,pair,side,qty,price,cost,fee,fee_currency FROM exchange_fills ORDER BY ts,trade_id'):
            ident, order, ts, pair, side, qty, price, cost, fee, currency = row
            eligible = [r for r in rates if 0 <= ts-r[0] <= 72*3600]
            rate = eligible[-1] if eligible else None
            if rate:
                extra = [str(rate[1]), datetime.fromtimestamp(rate[0], timezone.utc).isoformat(), rate[2],
                         str(Decimal(cost)*rate[1]), str(Decimal(fee)*rate[1]) if currency=='EUR' else '',
                         'Oppgitt observasjon før handelen; kontroller egnethet']
            else:
                missing += 1; extra = ['', '', '', '', '', 'Mangler egnet EUR/NOK-observasjon']
            w.writerow([ident,order,datetime.fromtimestamp(ts,timezone.utc).isoformat(),pair,side,qty,price,cost,fee,currency]+extra)
    return {'missing_fx': missing, 'file': str(destination),
            'scope': 'Botens utførelser. Inngangsverdi for andre kontoaktiviteter er ikke beregnet.'}
