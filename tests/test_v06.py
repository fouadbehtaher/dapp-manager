"""v0.6: provider boundary, on-chain confirmation, migration atomicity/alert baseline, CLI and dashboard API end to end."""
import io, json, os, sqlite3, tempfile, threading, unittest, urllib.request, urllib.error
from contextlib import redirect_stdout
from unittest import mock
from dappman import cli, chainrpc
from dappman.store import Store, SchemaError
from dappman.monitor import scan
from dappman.provider import Provider
from dappman.web import serve

ME = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40
NFT = '0x' + 'c' * 40
Z = '0x' + '0' * 40


def raw721(h, tid, frm=Z, to=ME, blk=10):
    return {'hash': h, 'blockNumber': str(blk), 'timeStamp': '1700000000', 'from': frm, 'to': to, 'contractAddress': NFT,
            'tokenID': str(tid), 'tokenSymbol': 'APE', 'tokenDecimal': '0'}


class FakeProvider(Provider):
    """A second provider implementation: proves monitor/history depend on the interface, not on Etherscan tool names."""
    name = 'fake'

    def __init__(self, nft_rows): self.nft_rows = nft_rows
    def balance(self, chain, address): return '0'
    def contract_creation(self, chain, address): return []
    def contract_source(self, chain, address): return []
    def tip_block(self, chain): return 100

    def events(self, kind, chain, address, *, contract=None, page=1, offset=50, sort='desc', startblock=None, endblock=None):
        if kind != 'erc721' or page > 1: return []
        rows = [r for r in self.nft_rows if (startblock is None or int(r['blockNumber']) >= startblock) and (endblock is None or int(r['blockNumber']) <= endblock)]
        return rows


class ProviderBoundary(unittest.TestCase):
    def test_scan_and_sync_use_provider_interface(self):
        from dappman.history import sync_target
        st = Store(':memory:'); t = st.add_target('wallet', '1', ME, 'w'); tgt = st.targets()[0]
        prov = FakeProvider([raw721('0xh', i) for i in (1, 2, 3)])
        res = scan(prov, st, tgt)
        assert res['snapshot']['provider'] == 'fake' and st.tx_count(t) == 3
        rep = sync_target(prov, st, tgt, kinds=('erc721',))
        assert st.tx_count(t) == 3 and rep['categories']['erc721']['status'] == 'synced to tip'
        assert rep['completeness'].startswith('unproven')


def rpc_opener(owner=ME, supports=True, revert=False, fail=False, chain_hex='0x1', bal=2):
    def opener(req, timeout=0):
        if fail: raise OSError('boom')
        body = json.loads(req.data.decode()); m = body['method']
        def ok(r): return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': r}).encode())
        if m == 'eth_blockNumber': return ok('0x64')
        if m == 'eth_chainId': return ok(chain_hex)
        data = body['params'][0]['data']
        assert body['params'][1] == '0x64'                       # every call pinned to the same block
        if data.startswith('0x01ffc9a7'): return ok('0x' + ('0' * 63) + ('1' if supports else '0'))
        if revert: return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': {'code': 3, 'message': 'execution reverted'}}).encode())
        if data.startswith('0x6352211e'): return ok('0x' + '0' * 24 + owner[2:])
        if data.startswith('0x00fdd58e'): return ok(hex(bal))
        raise AssertionError(data)
    return opener


class RpcConfirm(unittest.TestCase):
    def one(self, std='erc721', **kw):
        r = chainrpc.confirm_items('1', ME, [{'contract': NFT, 'token_id': '7', 'kind': std}], opener=rpc_opener(**kw))
        return r, r['results'][0] if r['results'] else None

    def test_confirmed_owner_pinned_with_host(self):
        r, x = self.one()
        assert x['state'] == 'confirmed_owner' and r['block'] == 100 and r['rpc'] and x['block'] == 100

    def test_other_owner_is_not_owner(self):
        assert self.one(owner=OTHER)[1]['state'] == 'not_owner'

    def test_revert_is_unknown_not_not_owner(self):
        x = self.one(revert=True)[1]
        assert x['state'] == 'unknown' and 'reverted' in x['reason']

    def test_unsupported_contract_is_unknown(self):
        assert self.one(supports=False)[1]['state'] == 'unknown'

    def test_provider_failure_leaves_everything_unconfirmed(self):
        r, x = self.one(fail=True)
        assert x is None and 'unavailable' in r['error'] and r['skipped'] == 1

    def test_wrong_chain_endpoint_rejected(self):
        r, x = self.one(chain_hex='0x89')
        assert 'chain' in r['error']

    def test_erc1155_balance(self):
        assert self.one('erc1155', bal=3)[1]['balance'] == '3'
        assert self.one('erc1155', bal=0)[1]['state'] == 'not_owner'

    def test_chain_without_rpc_is_unavailable(self):
        r = chainrpc.confirm_items('999999', ME, [{'contract': NFT, 'token_id': '1', 'kind': 'erc721'}], opener=rpc_opener())
        assert 'no public RPC' in r['error']


def old_db(path):
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT, dapp TEXT DEFAULT '', UNIQUE(chain,address));
    CREATE TABLE txs(target_id INT, hash TEXT, block INT, ts INT, frm TEXT, to_ TEXT, value TEXT, failed INT, method TEXT, kind TEXT, token TEXT,
      contract TEXT DEFAULT '', token_id TEXT DEFAULT '', PRIMARY KEY(target_id,hash,kind,frm,to_,value,token));
    CREATE TABLE settings(key TEXT PRIMARY KEY, val TEXT);
    CREATE TABLE snapshots(id INTEGER PRIMARY KEY, target_id INT, ts INT, data TEXT);
    CREATE TABLE alerts(id INTEGER PRIMARY KEY, target_id INT, ts INT, level TEXT, code TEXT, msg TEXT);
    CREATE TABLE gas(chain TEXT, ts INT, safe REAL, propose REAL, fast REAL, base REAL, block INT);""")
    db.execute("INSERT INTO targets(id,kind,chain,address,label) VALUES(1,'wallet','1',?,'w')", (ME,))
    db.execute("INSERT INTO txs VALUES(1,'0xold',10,1,?,?,'1',0,'','erc721','APE|','%s','1')" % NFT, (Z, ME))
    db.execute("INSERT INTO snapshots(target_id,ts,data) VALUES(1,1,'{\"status\":{}}')")
    db.commit(); db.close()


class Migration(unittest.TestCase):
    def test_failure_mid_migration_leaves_old_db_intact_and_retry_works(self):
        p = os.path.join(tempfile.mkdtemp(), 'o.db'); old_db(p)
        import dappman.store as S
        orig = S.TXS_V2
        S.TXS_V2 = orig + '; CREATE TABLE ((broken'           # fails after the rename inside the transaction
        try:
            with self.assertRaises(Exception): Store(p)
        finally:
            S.TXS_V2 = orig
        db = sqlite3.connect(p)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM txs').fetchone()[0] == 1           # original table still there, rename rolled back
        db.close()
        s = Store(p)                                                                # retry succeeds
        assert s.legacy_rows(1) == 1 and s.tx_count(1) == 0

    def test_no_alert_flood_after_migration_then_resync_keeps_baseline(self):
        p = os.path.join(tempfile.mkdtemp(), 'o.db'); old_db(p)
        s = Store(p); tgt = s.targets()[0]
        prov = FakeProvider([raw721('0xold', 1)])                                   # same tx the legacy DB already saw
        r1 = scan(prov, s, tgt)
        assert not [a for a in r1['new_alerts'] if a[1] == 'new_nft_transfers']     # legacy hashes still suppress "new"
        from dappman.history import sync_target
        sync_target(prov, s, tgt, kinds=('erc721',))                                # resync rebuilds under the new identity
        assert s.tx_count(1) == 1
        r2 = scan(prov, s, tgt)
        assert r2['new_alerts'] == []                                               # nothing new after resync
        prov2 = FakeProvider([raw721('0xold', 1), raw721('0xnew', 2, blk=11)])
        r3 = scan(prov2, s, tgt)
        assert [a[1] for a in r3['new_alerts']] == ['new_nft_transfers']            # a real new event still alerts


def run_cli(args, client=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(args, client=client)
    return rc, buf.getvalue()


class FakeClient:
    """Etherscan-shaped client for the CLI: 3 NFTs minted in one tx plus two identical ERC-20 twins."""
    def call(self, tool, a):
        if tool == 'get_block_by_timestamp': return {'blockNumber': '100'}
        if tool == 'get_native_balance': return '1000000000000000000'
        if tool == 'get_token_transfers' and a['standard'] == 'erc721' and a['page'] == 1:
            return [raw721('0xmint', i) for i in (1, 2, 3)]
        if tool == 'get_token_transfers' and a['standard'] == 'erc20' and a['page'] == 1:
            return [{'hash': '0xtw', 'blockNumber': '11', 'timeStamp': '1700000000', 'from': OTHER, 'to': ME, 'value': '5',
                     'contractAddress': '0x' + 'd' * 40, 'tokenSymbol': 'U', 'tokenDecimal': '6'}] * 2
        return []


class CliE2E(unittest.TestCase):
    def test_sync_history_nfts_confirm_flow(self):
        db = os.path.join(tempfile.mkdtemp(), 'e.db')
        base = ['--db', db]
        rc, out = run_cli(base + ['sync', ME, '--chain', '1', '--categories', 'erc721,erc20'], FakeClient())
        assert rc == 0 and 'completeness: unproven' in out and 'identical' in out or 'ambiguous' in out
        rc, out = run_cli(base + ['history', ME, '--chain', '1', '--json', '--limit', '50'], FakeClient())
        d = json.loads(out)
        assert len([r for r in d['rows'] if r['kind'] == 'erc721']) == 3 and len([r for r in d['rows'] if r['kind'] == 'erc20']) == 2
        assert d['identity_ambiguous_rows'] == 2 and d['completeness'] == 'unproven'
        with mock.patch('urllib.request.urlopen', rpc_opener()):
            rc, out = run_cli(base + ['nfts', ME, '--chains', '1', '--confirm', '--json'], FakeClient())
        res = json.loads(out)[0]
        oc = res['onchain_confirmation']
        assert oc['block'] == 100 and len(oc['results']) == 3 and all(r['state'] == 'confirmed_owner' for r in oc['results'])
        assert res['unconfirmed'] is False

    def test_nfts_confirm_failure_keeps_unconfirmed(self):
        db = os.path.join(tempfile.mkdtemp(), 'e.db')
        with mock.patch('urllib.request.urlopen', rpc_opener(fail=True)):
            rc, out = run_cli(['--db', db, 'nfts', ME, '--chains', '1', '--confirm', '--json'], FakeClient())
        r = json.loads(out)[0]
        assert r['unconfirmed'] is True and 'unavailable' in r['onchain_confirmation']['error']


class DashboardApi(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), 'w.db')
        s = Store(self.db); t = s.add_target('wallet', '1', ME, 'w')
        s.add_txs(t, [{'hash': '0xtw', 'block': 1, 'ts': 1, 'frm': OTHER, 'to': ME, 'value': '5', 'failed': 0, 'kind': 'erc20',
                       'token': 'U|6', 'contract': '0xu'}] * 2)
        self.srv = serve(lambda: Store(self.db), lambda: FakeClient(), port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self): self.srv.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), headers=headers or {})
        return urllib.request.urlopen(req, timeout=5)

    def test_state_exposes_ambiguity_and_legacy_counts(self):
        d = json.loads(self.get('/api/state').read())
        t = d['targets'][0]
        assert t['ambiguous_rows'] == 2 and t['legacy_rows'] == 0 and t['tx_count'] == 2

    def test_html_has_csp_and_warning_hooks(self):
        r = self.get('/')
        assert "script-src 'nonce-" in r.headers['Content-Security-Policy']
        html = r.read().decode()
        assert 't.ambiguous_rows' in html and 't.legacy_rows' in html

    def test_post_scan_requires_custom_header_and_rejects_foreign_host(self):
        req = urllib.request.Request('http://127.0.0.1:%d/api/scan' % self.port, data=b'', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req, timeout=5)
        assert e.exception.code == 403
        req = urllib.request.Request('http://127.0.0.1:%d/api/state' % self.port, headers={'Host': 'evil.example'})
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req, timeout=5)
        assert e.exception.code == 403

    def test_post_scan_with_header_runs(self):
        req = urllib.request.Request('http://127.0.0.1:%d/api/scan' % self.port, data=b'', method='POST', headers={'X-Dappman': '1'})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())['scanned'] == 1


class McpTools(unittest.TestCase):
    def test_confirm_tool_listed_and_readonly_store_guarded(self):
        from dappman.mcpserver import make_tools, handle
        db = os.path.join(tempfile.mkdtemp(), 'm.db'); s = Store(db); s.add_target('wallet', '1', ME, 'w'); s.db.commit()
        tools = make_tools(lambda: Store(db, readonly=True), lambda: FakeClient())
        names = [t['name'] for t in handle(tools, {'id': 1, 'method': 'tools/list'})['result']['tools']]
        assert 'dappman_confirm_nfts' in names
        with mock.patch('urllib.request.urlopen', rpc_opener()):
            r = handle(tools, {'id': 2, 'method': 'tools/call', 'params': {'name': 'dappman_confirm_nfts', 'arguments': {'address': ME}}})
        assert json.loads(r['result']['content'][0]['text'])['confirmations'][0]['block'] == 100


if __name__ == '__main__':
    unittest.main()


class Notifier(unittest.TestCase):
    def test_scan_writes_only_alerts_at_or_above_min_level_to_local_file(self):
        db = os.path.join(tempfile.mkdtemp(), 'n.db'); nf = os.path.join(tempfile.mkdtemp(), 'a.jsonl')
        class C(FakeClient):
            n = 0
            def call(self, tool, a):
                if tool == 'get_transactions' and a['page'] == 1:
                    C.n += 1
                    rows = [{'hash': '0xbig%d' % i, 'blockNumber': str(10 + i), 'timeStamp': '1700000000', 'from': OTHER, 'to': ME,
                             'value': str(50 * 10**18), 'isError': '0'} for i in range(C.n)]
                    return rows
                return super().call(tool, a)
        run_cli(['--db', db, 'add', ME, '--kind', 'wallet'])
        cl = C()
        run_cli(['--db', db, 'scan', '--notify-file', nf], cl)            # baseline: no alerts, no file
        assert not os.path.exists(nf)
        run_cli(['--db', db, 'scan', '--notify-file', nf], cl)            # one new tx with 50 ETH: info new_tx + warn large_value
        lines = [json.loads(x) for x in open(nf)]
        assert [x['code'] for x in lines] == ['large_value'] and lines[0]['address'] == ME     # info filtered out at default warn
        from dappman.notify import FileNotifier
        with self.assertRaises(ValueError): FileNotifier(nf, 'loud')


class NotifierErrors(unittest.TestCase):
    def test_url_target_refused_and_unwritable_path_does_not_lose_alert_or_crash(self):
        db = os.path.join(tempfile.mkdtemp(), 'n.db')
        run_cli(['--db', db, 'add', ME, '--kind', 'wallet'])
        rc, out = run_cli(['--db', db, 'scan', '--notify-file', 'https://example.test/x'], FakeClient())
        assert rc == 2 and 'local file path' in out

        class C(FakeClient):
            n = 0
            def call(self, tool, a):
                if tool == 'get_transactions' and a['page'] == 1:
                    C.n += 1
                    return [{'hash': '0xb%d' % i, 'blockNumber': str(10 + i), 'timeStamp': '1700000000', 'from': OTHER, 'to': ME,
                             'value': str(50 * 10**18), 'isError': '0'} for i in range(C.n)]
                return super().call(tool, a)
        cl = C(); bad = os.path.join(tempfile.mkdtemp(), 'missing-dir', 'a.jsonl')
        run_cli(['--db', db, 'scan', '--notify-file', bad], cl)
        rc, out = run_cli(['--db', db, 'scan', '--notify-file', bad], cl)
        assert rc == 0 and 'notify-file write failed' in out
        assert any(a['code'] == 'large_value' for a in Store(db).alerts(10))          # alert still recorded in the DB
        good = os.path.join(tempfile.mkdtemp(), 'a.jsonl')
        for _ in range(2):                                                            # retrying the same scan state adds nothing new
            run_cli(['--db', db, 'scan', '--notify-file', good], FakeClient())
        assert not os.path.exists(good)


class DashboardConfirmations(unittest.TestCase):
    def test_state_shows_saved_confirmation_without_calling_rpc(self):
        db = os.path.join(tempfile.mkdtemp(), 'c.db'); s = Store(db); t = s.add_target('wallet', '1', ME, 'w')
        s.add_txs(t, [{'hash': '0xh', 'block': 1, 'ts': 1, 'frm': Z, 'to': ME, 'value': '1', 'failed': 0, 'kind': 'erc721',
                       'token': 'APE|0', 'contract': NFT, 'token_id': '7'}])
        from dappman.web import state
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('dashboard must not call RPC')):
            item = state(Store(db))['targets'][0]['nft']['items'][0]
            assert item['confirmation'] is None
            s.save_confirmations(t, chainrpc.confirm_items('1', ME, [{'contract': NFT, 'token_id': '7', 'kind': 'erc721'}], opener=rpc_opener(owner=OTHER)))
            item = state(Store(db))['targets'][0]['nft']['items'][0]
        c = item['confirmation']
        assert c['state'] == 'not_owner' and c['block'] == 100 and c['rpc']


class ConfirmPersistsEndToEnd(unittest.TestCase):
    def test_cli_confirm_is_saved_and_dashboard_state_shows_it(self):
        db = os.path.join(tempfile.mkdtemp(), 'p.db')
        with mock.patch('urllib.request.urlopen', rpc_opener(owner=OTHER)):
            run_cli(['--db', db, 'nfts', ME, '--chains', '1', '--confirm', '--json'], FakeClient())
        from dappman.web import state
        items = state(Store(db))['targets'][0]['nft']['items']
        assert items and all(i['confirmation']['state'] == 'not_owner' and i['confirmation']['block'] == 100 for i in items)


class TwinsAcrossPages(unittest.TestCase):
    def test_twin_events_split_over_a_page_boundary_are_all_kept_and_resync_is_idempotent(self):
        from dappman.history import sync_target
        import dappman.history as H
        twin = lambda: raw721('0xtwin', 5, frm=ME, to=OTHER, blk=10)
        rows = [raw721('0xa%d' % i, i, blk=5) for i in range(48)] + [twin() for _ in range(4)]   # 48 singles then 4 identical twins: page 1 ends mid-group
        class P(FakeProvider):
            def events(self, kind, chain, address, *, contract=None, page=1, offset=50, sort='desc', startblock=None, endblock=None):
                if kind != 'erc721': return []
                return rows[(page - 1) * offset: page * offset]
        st = Store(':memory:'); t = st.add_target('wallet', '1', ME, 'w'); tgt = st.targets()[0]
        sync_target(P(rows), st, tgt, kinds=('erc721',), window=1000)
        assert st.tx_count(t) == 52 and st.ambiguous_rows(t) == 4
        st.set_setting(f'sync_{t}_erc721', -1)
        sync_target(P(rows), st, tgt, kinds=('erc721',), window=1000)                          # fresh session: same ordinals, nothing added
        assert st.tx_count(t) == 52

    def test_later_fetch_returning_more_twins_adds_them_fewer_removes_nothing(self):
        s, t = mk()
        mk4 = lambda n: [row('erc20', '0xh', value='5', token='U|6', contract='0xu', token_id='') for _ in range(n)]
        s.add_txs(t, mk4(3)); assert s.tx_count(t) == 3
        s.add_txs(t, mk4(4)); assert s.tx_count(t) == 4
        s.add_txs(t, mk4(2)); assert s.tx_count(t) == 4


def mk():
    s = Store(':memory:'); return s, s.add_target('wallet', '1', ME, 'w')


def row(kind, h, **kw):
    d = dict(hash=h, block=1, ts=1, frm=Z, to=ME, value='1', failed=0, kind=kind, token='APE|0', contract='0xc', token_id='1')
    d.update(kw); return d


class ConfirmationCache(unittest.TestCase):
    def test_new_transfer_invalidates_only_that_token_and_other_wallet_chain_do_not_share(self):
        s = Store(':memory:'); t1 = s.add_target('wallet', '1', ME, 'a'); t2 = s.add_target('wallet', '137', ME, 'a-poly'); t3 = s.add_target('wallet', '1', OTHER, 'b')
        for t in (t1, t2, t3):
            s.add_txs(t, [row('erc721', '0xh1', token_id='7'), row('erc721', '0xh2', token_id='8')])
        for t in (t1, t2, t3):
            s.save_confirmations(t, {'results': [{'contract': '0xc', 'token_id': '7', 'standard': 'erc721', 'state': 'confirmed_owner', 'block': 100, 'rpc': 'h'},
                                                 {'contract': '0xc', 'token_id': '8', 'standard': 'erc721', 'state': 'confirmed_owner', 'block': 100, 'rpc': 'h'}]})
        s.add_txs(t1, [row('erc721', '0xnew', token_id='7', frm=ME, to=OTHER, block=200)])          # new transfer of token 7 on chain 1 / wallet a
        assert ('0xc', '7') not in s.confirmations(t1) and ('0xc', '8') in s.confirmations(t1)
        assert ('0xc', '7') in s.confirmations(t2) and ('0xc', '7') in s.confirmations(t3)           # other chain / other wallet untouched
        s.add_txs(t1, [row('erc721', '0xnew', token_id='7', frm=ME, to=OTHER, block=200)])          # resync of the same row: no extra invalidation needed
        s.save_confirmations(t1, {'results': [{'contract': '0xc', 'token_id': '7', 'standard': 'erc721', 'state': 'not_owner', 'block': 300, 'rpc': 'h'}]})
        s.add_txs(t1, [row('erc721', '0xnew', token_id='7', frm=ME, to=OTHER, block=200)])
        assert ('0xc', '7') in s.confirmations(t1)                                                   # idempotent resync keeps a newer check

    def test_dashboard_marks_check_stale_if_a_later_block_row_exists(self):
        db = os.path.join(tempfile.mkdtemp(), 's.db'); s = Store(db); t = s.add_target('wallet', '1', ME, 'a')
        s.add_txs(t, [row('erc721', '0xh1', token_id='7', block=50)])
        s.save_confirmations(t, {'results': [{'contract': '0xc', 'token_id': '7', 'standard': 'erc721', 'state': 'confirmed_owner', 'block': 100, 'rpc': 'h'}]})
        from dappman.web import state
        assert state(Store(db))['targets'][0]['nft']['items'][0]['confirmation']['stale'] is False
        s.db.execute("INSERT OR IGNORE INTO txs(target_id,kind,hash,ident,ord,block,ts,frm,to_,value,failed,method,token,contract,token_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (t, 'erc721', '0xlate', 'x', 0, 150, 1, Z, ME, '1', 0, '', 'APE|0', '0xc', '7')); s.db.commit()
        assert state(Store(db))['targets'][0]['nft']['items'][0]['confirmation']['stale'] is True


class Backup(unittest.TestCase):
    def test_backup_made_verified_wal_included_and_restorable(self):
        import shutil, glob
        d = tempfile.mkdtemp(); p = os.path.join(d, 'w.db'); old_db(p)
        live = sqlite3.connect(p); live.execute('PRAGMA journal_mode=WAL'); live.execute("INSERT INTO settings VALUES('wal_marker','1')"); live.commit()   # content may sit only in the -wal file
        s = Store(p)
        baks = glob.glob(p + '.pre-v2-*.bak'); assert len(baks) == 1
        b = sqlite3.connect(baks[0])
        assert b.execute("SELECT val FROM settings WHERE key='wal_marker'").fetchone()[0] == '1'      # WAL content captured
        assert b.execute('PRAGMA user_version').fetchone()[0] == 0 and b.execute('SELECT COUNT(*) FROM txs').fetchone()[0] == 1
        b.close(); live.close()
        r = os.path.join(d, 'restored.db'); shutil.copy(baks[0], r)                                      # restore = copy back
        rs = sqlite3.connect(r); assert rs.execute('SELECT COUNT(*) FROM txs').fetchone()[0] == 1 and rs.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert Store(r).legacy_rows(1) == 1                                                              # and the restored copy migrates again cleanly
        assert Store(p).tx_count(1) == 0 and len(glob.glob(p + '.pre-v2-*.bak')) == 1                    # already migrated: no second backup

    def test_backup_failure_blocks_migration(self):
        p = os.path.join(tempfile.mkdtemp(), 'f.db'); old_db(p)
        import dappman.store as S
        orig = sqlite3.connect
        def deny(path, *a, **k):
            if str(path).endswith('.bak'): raise sqlite3.OperationalError('read-only file system')
            return orig(path, *a, **k)
        with mock.patch.object(S.sqlite3, 'connect', side_effect=deny):
            with self.assertRaises(S.BackupError): Store(p)
        db = sqlite3.connect(p)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0 and db.execute('SELECT COUNT(*) FROM txs').fetchone()[0] == 1   # untouched
        assert not [x for x in os.listdir(os.path.dirname(p)) if x.endswith('.bak')]


class MalformedProviderReplies(unittest.TestCase):
    def client(self, reply):
        from dappman.client import McpClient
        c = McpClient(sleep=0, retries=1)
        c.sid = 'x'
        c._rpc = lambda *a, **k: reply
        return c

    def test_garbage_is_capped_sanitised_and_never_crashes(self):
        from dappman.client import ApiError
        huge = 'A' * 5000 + '\x1b[31m\x00 injected \n\r' + 'B' * 5000
        cases = [{'result': {'content': [{'text': json.dumps({'ok': False, 'status': '0', 'message': huge})}]}},
                 {'result': {'content': [{'text': json.dumps([1, 2, 3])}]}},
                 {'result': {'content': [{'text': 'not json ' + huge}]}},
                 {'result': {}}, {}, {'result': {'content': []}},
                 {'result': {'content': [{'text': json.dumps({'ok': True, 'status': '0', 'result': huge})}]}}]
        for r in cases:
            with self.assertRaises(ApiError) as e:
                self.client(r).call('get_transactions', {})
            m = str(e.exception)
            assert len(m) <= 220 and '\x1b' not in m and '\x00' not in m and '\n' not in m, m[:80]

    def test_rpc_garbage_gives_unknown_not_not_owner(self):
        def op(req, timeout=0):
            body = json.loads(req.data.decode())
            if body['method'] == 'eth_blockNumber': return io.BytesIO(b'{"result":"0x64"}')
            if body['method'] == 'eth_chainId': return io.BytesIO(b'{"result":"0x1"}')
            return io.BytesIO(b'<html>' + b'Z' * 4000 + b'</html>')
        r = chainrpc.confirm_items('1', ME, [{'contract': NFT, 'token_id': '7', 'kind': 'erc721'}], opener=op)
        x = r['results'][0]
        assert x['state'] == 'unknown' and len(x['reason']) < 160
