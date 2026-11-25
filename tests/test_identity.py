"""Regression tests for event identity (v0.5 collapsed distinct events into one row)."""
import sqlite3, tempfile, os
from dappman.store import Store, SchemaError
from dappman.nfts import derive
from dappman.monitor import _tx

ME = '0x' + 'a' * 40
Z = '0x' + '0' * 40


def row(kind, h, **kw):
    d = dict(hash=h, block=1, ts=1, frm=Z, to=ME, value='1', failed=0, kind=kind, token='APE|0', contract='0xc', token_id='1')
    d.update(kw); return d


def mk():
    s = Store(':memory:'); return s, s.add_target('wallet', '1', ME, 'w')


def test_batch_mint_keeps_every_token():
    s, t = mk()
    assert s.add_txs(t, [row('erc721', '0xh', token_id=str(i)) for i in (1, 2, 3)]) == 3
    assert len(derive(s, t, ME)['items']) == 3


def test_same_events_other_chain_target_do_not_collide():
    s, t = mk(); t2 = s.add_target('wallet', '137', ME, 'w2')
    s.add_txs(t, [row('erc721', '0xh')]); s.add_txs(t2, [row('erc721', '0xh')])
    assert s.tx_count(t) == 1 and s.tx_count(t2) == 1


def test_internal_rows_distinguished_by_trace_id():
    s, t = mk()
    rs = [row('internal', '0xh', trace=tr, value='5', token='') for tr in ('0_1', '0_2')]
    assert s.add_txs(t, rs) == 2 and s.ambiguous_rows(t) == 0


def test_identical_erc20_twins_kept_and_flagged_ambiguous():
    s, t = mk()
    tw = [row('erc20', '0xh', value='5', token='USDC|6', contract='0xu', token_id='') for _ in range(2)]
    assert s.add_txs(t, tw) == 2
    assert s.ambiguous_rows(t) == 2


def test_resync_is_idempotent():
    s, t = mk()
    rs = [row('erc721', '0xh', token_id=str(i)) for i in (1, 2)] + [row('erc20', '0xh', value='5', token='U|6', contract='0xu', token_id='')] * 2
    assert s.add_txs(t, rs) == 4
    assert s.add_txs(t, rs) == 0 and s.tx_count(t) == 4


def test_erc1155_same_token_different_amounts_distinct():
    s, t = mk()
    assert s.add_txs(t, [row('erc1155', '0xh', value='2'), row('erc1155', '0xh', value='3')]) == 2
    assert derive(s, t, ME)['items'][0]['qty'] == 5


def test_tx_mapper_carries_trace_and_erc20_contract():
    r = _tx({'hash': '0xh', 'traceId': '0_1', 'from': ME, 'to': ME, 'value': '1', 'blockNumber': '1', 'timeStamp': '1'}, 'internal')
    assert r['trace'] == '0_1'
    r = _tx({'hash': '0xh', 'contractAddress': '0xABC', 'from': ME, 'to': ME, 'value': '1', 'blockNumber': '1', 'timeStamp': '1', 'tokenSymbol': 'U', 'tokenDecimal': '6'}, 'erc20')
    assert r['contract'] == '0xabc'


def test_migration_from_v05_preserves_legacy_and_requires_resync():
    p = os.path.join(tempfile.mkdtemp(), 'old.db')
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT, dapp TEXT DEFAULT '', UNIQUE(chain,address));
    CREATE TABLE txs(target_id INT, hash TEXT, block INT, ts INT, frm TEXT, to_ TEXT, value TEXT, failed INT, method TEXT, kind TEXT, token TEXT,
      contract TEXT DEFAULT '', token_id TEXT DEFAULT '', PRIMARY KEY(target_id,hash,kind,frm,to_,value,token));
    CREATE TABLE settings(key TEXT PRIMARY KEY, val TEXT);
    INSERT INTO targets(id,kind,chain,address,label) VALUES(1,'wallet','1','%s','w');
    INSERT INTO txs VALUES(1,'0xold',1,1,'%s','%s','1',0,'','erc721','APE|','0xc','1');
    INSERT INTO settings VALUES('sync_1_erc721','999');""" % (ME, Z, ME))
    db.commit(); db.close()
    s = Store(p)
    assert s.tx_count(1) == 0 and s.legacy_rows(1) == 1          # not counted as trusted history
    assert ('0xold', 'erc721') in s.known_hashes(1)              # but still suppresses "new tx" floods
    assert s.get_setting('sync_1_erc721') is None                # checkpoints reset: sync refetches everything
    assert derive(s, 1, ME)['legacy_rows_excluded'] == 1
    s.add_txs(1, [row('erc721', '0xnew')]); Store(p)             # reopen: migration is not repeated
    assert s.tx_count(1) == 1


def test_readonly_store_refuses_unmigrated_db():
    p = os.path.join(tempfile.mkdtemp(), 'x.db'); sqlite3.connect(p).execute('create table t(a)').connection.commit()
    try:
        Store(p, readonly=True); assert False
    except SchemaError:
        pass
