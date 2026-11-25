import json, sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT,
  UNIQUE(chain,address));
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, target_id INT, ts INT, data TEXT);
CREATE TABLE IF NOT EXISTS txs(target_id INT, hash TEXT, block INT, ts INT, frm TEXT, to_ TEXT, value TEXT,
  failed INT, method TEXT, kind TEXT, token TEXT, PRIMARY KEY(target_id,hash,kind,frm,to_,value,token));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, target_id INT, ts INT, level TEXT, code TEXT, msg TEXT);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add_target(self, kind, chain, address, label):
        self.db.execute('INSERT OR IGNORE INTO targets(kind,chain,address,label) VALUES(?,?,?,?)',
                        (kind, chain, address, label))
        self.db.commit()
        return self.db.execute('SELECT id FROM targets WHERE chain=? AND address=?', (chain, address)).fetchone()[0]

    def remove_target(self, chain, address):
        r = self.db.execute('SELECT id FROM targets WHERE chain=? AND address=?', (chain, address)).fetchone()
        if not r: return False
        for t in ('snapshots', 'txs', 'alerts'):
            self.db.execute(f'DELETE FROM {t} WHERE target_id=?', (r[0],))
        self.db.execute('DELETE FROM targets WHERE id=?', (r[0],))
        self.db.commit(); return True

    def targets(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM targets ORDER BY id')]

    def last_snapshot(self, tid):
        r = self.db.execute('SELECT * FROM snapshots WHERE target_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
        return (r['ts'], json.loads(r['data'])) if r else (None, None)

    def save_snapshot(self, tid, data):
        self.db.execute('INSERT INTO snapshots(target_id,ts,data) VALUES(?,?,?)', (tid, int(time.time()), json.dumps(data)))
        self.db.commit()

    def known_hashes(self, tid):
        return {(r[0], r[1]) for r in self.db.execute('SELECT hash,kind FROM txs WHERE target_id=?', (tid,))}

    def add_txs(self, tid, rows):
        n = 0
        for r in rows:
            c = self.db.execute('INSERT OR IGNORE INTO txs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (tid, r['hash'], r['block'], r['ts'], r['frm'], r['to'], r['value'], r['failed'],
                 r.get('method', ''), r['kind'], r.get('token', '')))
            n += c.rowcount
        self.db.commit(); return n

    def add_alert(self, tid, level, code, msg):
        self.db.execute('INSERT INTO alerts(target_id,ts,level,code,msg) VALUES(?,?,?,?,?)',
                        (tid, int(time.time()), level, code, msg))
        self.db.commit()

    def alerts(self, limit=100):
        return [dict(r) for r in self.db.execute(
            'SELECT a.*, t.label, t.address, t.chain FROM alerts a JOIN targets t ON t.id=a.target_id ORDER BY a.id DESC LIMIT ?', (limit,))]

    def recent_txs(self, tid, limit=25):
        return [dict(r) for r in self.db.execute('SELECT * FROM txs WHERE target_id=? ORDER BY ts DESC LIMIT ?', (tid, limit))]

    def tx_count(self, tid):
        return self.db.execute('SELECT COUNT(*) FROM txs WHERE target_id=?', (tid,)).fetchone()[0]
