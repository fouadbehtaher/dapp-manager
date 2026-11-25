import json, os, sqlite3, sys, time
from .events import event_ident

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT, dapp TEXT DEFAULT '',
  UNIQUE(chain,address));
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, target_id INT, ts INT, data TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, val TEXT);
CREATE TABLE IF NOT EXISTS gas(chain TEXT, ts INT, safe REAL, propose REAL, fast REAL, base REAL, block INT);
CREATE TABLE IF NOT EXISTS nft_confirmations(target_id INT, contract TEXT, token_id TEXT, standard TEXT, state TEXT, reason TEXT,
  block INT, rpc TEXT, ts INT, PRIMARY KEY(target_id,contract,token_id));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, target_id INT, ts INT, level TEXT, code TEXT, msg TEXT);
"""


TXS_V2 = """
CREATE TABLE txs(target_id INT, kind TEXT, hash TEXT, ident TEXT, ord INT, block INT, ts INT, frm TEXT, to_ TEXT,
  value TEXT, failed INT, method TEXT, token TEXT, contract TEXT DEFAULT '', token_id TEXT DEFAULT '',
  dup_group INT DEFAULT 1, PRIMARY KEY(target_id,kind,hash,ident,ord));
"""
SCHEMA_VERSION = 2


class SchemaError(Exception):
    pass


class BackupError(Exception):
    pass


def _tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


class Store:
    def __init__(self, path, readonly=False):
        if readonly:  # MCP server: guaranteed no writes even if a bug tried one
            self.db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if readonly:
            v = self.db.execute('PRAGMA user_version').fetchone()[0]
            if v != SCHEMA_VERSION:
                raise SchemaError('database schema v%d, tool needs v%d: run `python3 -m dappman --db <file> scan` once to migrate, then `python3 -m dappman sync ADDRESS --chain N` per wallet to rebuild history' % (v, SCHEMA_VERSION))
            return
        self._backup_before_migration(path)
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(targets)')]
        if 'dapp' not in cols:  # databases created by v0.3
            self.db.execute("ALTER TABLE targets ADD COLUMN dapp TEXT DEFAULT ''"); self.db.commit()
        self._migrate()

    def _backup_before_migration(self, path):
        """Consistent copy (SQLite online backup API, which includes WAL content) taken BEFORE any schema change, then
        verified (integrity_check + per-table row counts). If the copy cannot be made or verified the migration does not run."""
        if path in (':memory:', '') or not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        v = self.db.execute('PRAGMA user_version').fetchone()[0]
        if v >= SCHEMA_VERSION or 'txs' not in _tables(self.db):
            return None
        base = '%s.pre-v%d-%s' % (path, SCHEMA_VERSION, time.strftime('%Y%m%d-%H%M%S'))
        dest, i = base + '.bak', 1
        while os.path.exists(dest):
            dest = '%s-%d.bak' % (base, i); i += 1
        try:
            dst = sqlite3.connect(dest)
            try:
                self.db.backup(dst)
            finally:
                dst.close()
            chk = sqlite3.connect('file:%s?mode=ro' % dest, uri=True)
            try:
                if chk.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise BackupError('backup failed integrity_check')
                src_t = _tables(self.db)
                if _tables(chk) != src_t:
                    raise BackupError('backup is missing tables')
                for t in src_t:
                    if chk.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0] != self.db.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]:
                        raise BackupError('backup row count differs for table ' + t)
            finally:
                chk.close()
            try: os.chmod(dest, os.stat(path).st_mode & 0o777)
            except OSError: pass
        except Exception as e:
            try: os.remove(dest)
            except OSError: pass
            raise BackupError('pre-migration backup failed, database left untouched: ' + str(e)[:150])
        print('dappman: backed up the old database to %s before migrating' % dest, file=sys.stderr)
        return dest

    def _migrate(self):
        """Atomic: one transaction, so a crash or error mid-migration leaves the old database untouched."""
        v = self.db.execute('PRAGMA user_version').fetchone()[0]
        if v >= SCHEMA_VERSION:
            return
        has_txs = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='txs'").fetchone()
        self.db.commit()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if has_txs:
                # v0.3-0.5 keyed rows on (hash,kind,from,to,value,token): distinct events in one tx could collapse into one row.
                # Those rows cannot be repaired locally. They are kept (read-only, for alert de-duplication and audit) and
                # excluded from counts/NFT derivation until a sync re-fetches them under the new identity.
                self.db.execute('ALTER TABLE txs RENAME TO txs_legacy')
            for stmt in [x for x in TXS_V2.split(';') if x.strip()]:
                self.db.execute(stmt)
            if has_txs:
                n = self.db.execute('SELECT COUNT(*) FROM txs_legacy').fetchone()[0]
                self.db.execute("DELETE FROM settings WHERE key LIKE 'sync\\_%' ESCAPE '\\' OR key LIKE 'syncgaps\\_%' ESCAPE '\\'")
                self.db.execute("INSERT OR REPLACE INTO settings(key,val) VALUES('migrated_legacy_rows',?)", (str(n),))
            self.db.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def save_confirmations(self, tid, conf):
        """conf = chainrpc.confirm_items() result. Stored so the dashboard can show verified states without calling any RPC."""
        for r in conf.get('results', []):
            self.db.execute('INSERT OR REPLACE INTO nft_confirmations VALUES(?,?,?,?,?,?,?,?,?)',
                (tid, r['contract'], str(r['token_id']), r['standard'], r['state'], r.get('reason', ''), r['block'], r.get('rpc') or '', int(time.time())))
        self.db.commit()

    def confirmations(self, tid):
        try:
            return {(r['contract'], r['token_id']): dict(r) for r in self.db.execute('SELECT * FROM nft_confirmations WHERE target_id=?', (tid,))}
        except sqlite3.OperationalError:
            return {}

    def legacy_rows(self, tid):
        try:
            return self.db.execute('SELECT COUNT(*) FROM txs_legacy WHERE target_id=?', (tid,)).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def ambiguous_rows(self, tid):
        r = self.db.execute('SELECT COALESCE(SUM(c),0) FROM (SELECT COUNT(*) c FROM txs WHERE target_id=? GROUP BY kind,hash,ident HAVING COUNT(*)>1)', (tid,)).fetchone()
        return int(r[0])

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
        for t in ('snapshots', 'txs', 'alerts', 'nft_confirmations'):
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
        out = {(r[0], r[1]) for r in self.db.execute('SELECT hash,kind FROM txs WHERE target_id=?', (tid,))}
        if self.legacy_rows(tid):  # pre-migration rows still suppress "new tx" alerts for things already seen
            out |= {(r[0], r[1]) for r in self.db.execute('SELECT hash,kind FROM txs_legacy WHERE target_id=?', (tid,))}
        return out

    def add_txs(self, tid, rows, session=None):
        """Insert events; returns how many were new. Idempotent: re-fetching the same events adds nothing.
        Byte-identical events (same tx, same fields) get ordinals 0,1,... in arrival order. `session` is a dict that the
        caller keeps across the pages of ONE fetch, so twins split over a page boundary keep counting (0,1 | 2,3) instead of
        restarting at 0 and being dropped. A later fetch that returns more twins adds the extra ones; one that returns
        fewer removes nothing. The provider has no log index, so this is the best available identity, not a proof."""
        seen = session if session is not None else {}
        n = 0
        for r in rows:
            k = (r['kind'], r['hash'], event_ident(r))
            o = seen.get(k, 0); seen[k] = o + 1
            c = self.db.execute('INSERT OR IGNORE INTO txs(target_id,kind,hash,ident,ord,block,ts,frm,to_,value,failed,method,token,contract,token_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (tid, k[0], k[1], k[2], o, r['block'], r['ts'], r['frm'], r['to'], r['value'], r['failed'],
                 r.get('method', ''), r.get('token', ''), r.get('contract', ''), r.get('token_id', '')))
            n += c.rowcount
            if c.rowcount and r['kind'] in ('erc721', 'erc1155'):  # a new NFT event makes any saved on-chain check for that token stale
                self.db.execute('DELETE FROM nft_confirmations WHERE target_id=? AND contract=? AND token_id=?',
                                (tid, r.get('contract', ''), str(r.get('token_id', ''))))
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
