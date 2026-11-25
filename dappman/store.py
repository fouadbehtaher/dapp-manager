import json, sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT, dapp TEXT DEFAULT '',
  UNIQUE(chain,address));
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, target_id INT, ts INT, data TEXT);
CREATE TABLE IF NOT EXISTS txs(target_id INT, hash TEXT, block INT, ts INT, frm TEXT, to_ TEXT, value TEXT,
  failed INT, method TEXT, kind TEXT, token TEXT, PRIMARY KEY(target_id,hash,kind,frm,to_,value,token));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, val TEXT);
CREATE TABLE IF NOT EXISTS gas(chain TEXT, ts INT, safe REAL, propose REAL, fast REAL, base REAL, block INT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, target_id INT, ts INT, level TEXT, code TEXT, msg TEXT);
"""


class Store:
    def __init__(self, path, readonly=False):
        if readonly:  # MCP server: guaranteed no writes even if a bug tried one
            self.db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(targets)')]
        if 'dapp' not in cols:  # migrate databases created by v0.3
            self.db.execute("ALTER TABLE targets ADD COLUMN dapp TEXT DEFAULT ''"); self.db.commit()
        tcols = [r[1] for r in self.db.execute('PRAGMA table_info(txs)')]
        for col in ('contract', 'token_id'):  # migrate databases created by v0.4
            if col not in tcols:
                self.db.execute(f"ALTER TABLE txs ADD COLUMN {col} TEXT DEFAULT ''"); self.db.commit()

    def add_target(self, kind, chain, address, label, dapp=''):
        self.db.execute('INSERT OR IGNORE INTO targets(kind,chain,address,label,dapp) VALUES(?,?,?,?,?)',
                        (kind, chain, address, label, dapp))
        if dapp:
            self.db.execute('UPDATE targets SET dapp=? WHERE chain=? AND address=?', (dapp, chain, address))
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
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(txs)')]
        wide = 'contract' in cols
        n = 0
        for r in rows:
            if wide:
                c = self.db.execute('INSERT OR IGNORE INTO txs(target_id,hash,block,ts,frm,to_,value,failed,method,kind,token,contract,token_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (tid, r['hash'], r['block'], r['ts'], r['frm'], r['to'], r['value'], r['failed'],
                     r.get('method', ''), r['kind'], r.get('token', ''), r.get('contract', ''), r.get('token_id', '')))
            else:
                c = self.db.execute('INSERT OR IGNORE INTO txs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (tid, r['hash'], r['block'], r['ts'], r['frm'], r['to'], r['value'], r['failed'],
                     r.get('method', ''), r['kind'], r.get('token', '')))
            n += c.rowcount
        self.db.commit(); return n

    def nft_rows(self, tid):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM txs WHERE target_id=? AND kind IN ('erc721','erc1155') ORDER BY ts", (tid,))]

    def add_alert(self, tid, level, code, msg):
        self.db.execute('INSERT INTO alerts(target_id,ts,level,code,msg) VALUES(?,?,?,?,?)',
                        (tid, int(time.time()), level, code, msg))
        self.db.commit()

    def alerts(self, limit=100):
        return [dict(r) for r in self.db.execute(
            "SELECT a.*, COALESCE(t.label,'gas') AS label, COALESCE(t.address,'') AS address, COALESCE(t.chain,'') AS chain FROM alerts a LEFT JOIN targets t ON t.id=a.target_id ORDER BY a.id DESC LIMIT ?", (limit,))]

    def recent_txs(self, tid, limit=25):
        return [dict(r) for r in self.db.execute('SELECT * FROM txs WHERE target_id=? ORDER BY ts DESC LIMIT ?', (tid, limit))]

    def tx_count(self, tid):
        return self.db.execute('SELECT COUNT(*) FROM txs WHERE target_id=?', (tid,)).fetchone()[0]

    def window(self, tid):
        """All stored normal-tx rows for a target (the window we actually hold, not full history)."""
        out = []
        for r in self.db.execute("SELECT * FROM txs WHERE target_id=? AND kind='normal' ORDER BY ts DESC", (tid,)):
            d = dict(r); d['to'] = d['to_']; out.append(d)
        return out

    def get_setting(self, key, default=None):
        r = self.db.execute('SELECT val FROM settings WHERE key=?', (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key, val):
        self.db.execute('INSERT OR REPLACE INTO settings(key,val) VALUES(?,?)', (key, str(val))); self.db.commit()

    def add_gas(self, chain, safe, propose, fast, base, block):
        self.db.execute('INSERT INTO gas VALUES(?,?,?,?,?,?,?)', (chain, int(time.time()), safe, propose, fast, base, block)); self.db.commit()

    def gas_rows(self, chain, limit=288):
        return [dict(r) for r in self.db.execute('SELECT * FROM gas WHERE chain=? ORDER BY ts DESC LIMIT ?', (chain, limit))]
