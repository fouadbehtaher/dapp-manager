"""v0.5: bounded history sync, NFT derivation, OFAC sanctions, GoPlus reputation, local MCP server."""
import io, json, os, tempfile, unittest
from dappman.client import ApiError
from dappman.store import Store
from dappman.monitor import scan, _tx
from dappman import cli

A = '0x' + 'ab' * 20
B = '0x' + 'cd' * 20


class HistFake:
    """Paged fake: deterministic rows per (kind, block); blocks 0..99, one row per block per kind."""
    def __init__(self):
        self.calls = 0

    def call(self, tool, args):
        if tool == 'get_block_by_timestamp':
            return {'blockNumber': '99'}
        self.calls += 1
        if tool == 'get_token_transfers':
            kind = args['standard']
        else:
            kind = {'get_transactions': 'normal', 'get_internal_transactions': 'internal'}[tool]
        lo, hi = int(args['startblock']), int(args['endblock'])
        page, offset = int(args['page']), int(args['offset'])
        rows = [{'hash': f'0x{kind}{b}', 'blockNumber': str(b), 'timeStamp': str(1700000000 + b),
                 'from': B, 'to': A, 'value': '1000', 'isError': '0', 'tokenSymbol': 'T',
                 'contractAddress': '0x' + 'ee' * 20, 'tokenID': str(b), 'tokenValue': '2'} for b in range(lo, hi + 1)]
        s, e = (page - 1) * offset, page * offset
        return rows[s:e]


class HistoryT(unittest.TestCase):
    def mk(self):
        st = Store(':memory:')
        tid = st.add_target('wallet', '1', A, 'w')
        return st, [x for x in st.targets() if x['id'] == tid][0]

    def test_full_sync_checkpoints_and_resume(self):
        from dappman.history import sync_target
        st, t = self.mk()
        f = HistFake()
        rep = sync_target(f, st, t, _tx, kinds=('normal',), window=40)
        c = rep['categories']['normal']
        self.assertEqual(c['status'], 'synced to tip')
        self.assertEqual(c['synced_through'], 99)
        self.assertEqual(c['rows'], 100)                      # one per block 0..99
        self.assertEqual(st.tx_count(t['id']), 100)
        rep2 = sync_target(f, st, t, _tx, kinds=('normal',), window=40)
        self.assertEqual(rep2['categories']['normal']['status'], 'up to date')  # checkpoint held
        self.assertIn('bounded', rep['limits'])

    def test_stop_and_resume_keeps_rows(self):
        from dappman.history import sync_target
        st, t = self.mk()
        f = HistFake()
        rep = sync_target(f, st, t, _tx, kinds=('normal',), window=40, max_calls=2)
        self.assertTrue(rep['categories']['normal']['status'].startswith('stopped'))
        first = st.tx_count(t['id'])
        self.assertGreater(first, 0)
        rep2 = sync_target(f, st, t, _tx, kinds=('normal',), window=40)
        self.assertEqual(rep2['categories']['normal']['status'], 'synced to tip')
        self.assertEqual(st.tx_count(t['id']), 100)           # no duplicates on resume

    def test_window_shrinks_at_10k_cap_and_recovers(self):
        from dappman import history
        st, t = self.mk()

        class Busy(HistFake):
            def call(self, tool, args):
                # windows >= 40 blocks pretend to hold >10k rows (full pages forever, so the 200-page cap fires);
                # smaller windows answer normally -> proves the engine shrank the window and recovered
                if tool == 'get_transactions' and int(args['endblock']) - int(args['startblock']) + 1 >= 40:
                    return [{'hash': 'x', 'blockNumber': '1', 'timeStamp': '1', 'from': B, 'to': A, 'value': '0'}] * 50
                return super().call(tool, args)

        f = Busy()
        rep = history.sync_target(f, st, t, _tx, kinds=('normal',), window=40)
        c = rep['categories']['normal']
        self.assertEqual(c['status'], 'synced to tip')
        self.assertEqual(c.get('capped_windows'), 1)
        self.assertIn('shrunk', c['note'])

    def test_unknown_category_is_skipped_not_fatal(self):
        from dappman.history import sync_target
        st, t = self.mk()
        rep = sync_target(HistFake(), st, t, _tx, kinds=('normal', 'bogus'))
        self.assertIn('skipped', rep['categories']['bogus']['status'])


class NftT(unittest.TestCase):
    def test_derived_inventory_counts_and_burns(self):
        from dappman.nfts import derive
        st = Store(':memory:')
        tid = st.add_target('wallet', '1', A, 'w')
        C = '0x' + 'ee' * 20
        st.add_txs(tid, [
            _tx({'hash': '0x1', 'blockNumber': '1', 'timeStamp': '1', 'from': '0x' + '00' * 20, 'to': A,
                 'tokenSymbol': 'ART', 'contractAddress': C, 'tokenID': '7'}, 'erc721'),
            _tx({'hash': '0x2', 'blockNumber': '2', 'timeStamp': '2', 'from': A, 'to': B,
                 'tokenSymbol': 'ART', 'contractAddress': C, 'tokenID': '7'}, 'erc721'),   # sent out again
            _tx({'hash': '0x3', 'blockNumber': '3', 'timeStamp': '3', 'from': B, 'to': A,
                 'tokenSymbol': 'GAME', 'contractAddress': C, 'tokenID': '9', 'tokenValue': '5'}, 'erc1155'),
            _tx({'hash': '0x4', 'blockNumber': '4', 'timeStamp': '4', 'from': A, 'to': B,
                 'tokenSymbol': 'GAME', 'contractAddress': C, 'tokenID': '9', 'tokenValue': '2'}, 'erc1155'),
        ])
        d = derive(st, tid, A)
        self.assertEqual(d['derived_from_rows'], 4)
        inv = {(i['contract'], i['token_id']): i['qty'] for i in d['items']}
        self.assertNotIn((C, '7'), inv)                       # in then out -> not held
        self.assertEqual(inv[(C, '9')], 3)                    # 5 in - 2 out
        self.assertIn('unconfirmed', d['window'])


SDN_FIXTURE = '''<?xml version="1.0"?>
<sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
  <publshInformation><Publish_Date>01/02/2026</Publish_Date><Record_Count>1</Record_Count></publshInformation>
  <sdnEntry><uid>1</uid><firstName>EVIL</firstName><lastName>ACTOR</lastName><sdnType>Entity</sdnType>
    <idList><id><uid>2</uid><idType>Digital Currency Address - ETH</idType>
      <idNumber>''' + B + '''</idNumber></id>
           <id><uid>3</uid><idType>Passport</idType><idNumber>X123</idNumber></id></idList>
  </sdnEntry>
</sdnList>'''


class SanctionsT(unittest.TestCase):
    def _store_with_fixture(self, fetched=None):
        import time as _t
        from dappman import sanctions
        st = Store(':memory:')
        p = os.path.join(tempfile.mkdtemp(), 'sdn.xml')
        open(p, 'w').write(SDN_FIXTURE)
        d = sanctions.parse_sdn_xml(p)
        st.set_setting('ofac_sdn', json.dumps(d)); st.set_setting('ofac_sdn_fetched_at', fetched if fetched is not None else int(_t.time()))
        return st

    def test_parse_only_crypto_ids(self):
        from dappman import sanctions
        p = os.path.join(tempfile.mkdtemp(), 'sdn.xml')
        open(p, 'w').write(SDN_FIXTURE)
        d = sanctions.parse_sdn_xml(p)
        self.assertEqual(d['publish_date'], '01/02/2026')
        self.assertEqual(d['entries'][B]['entity'], 'EVIL ACTOR')
        self.assertEqual(d['entries'][B]['currencies'], ['eth'])
        self.assertNotIn('X123', d['entries'])                # passport is not a crypto address

    def test_three_distinct_states(self):
        from dappman import sanctions
        empty = Store(':memory:')
        self.assertEqual(sanctions.check(empty, B)['state'], 'unknown')        # fail-closed: no list = unknown
        st = self._store_with_fixture()
        self.assertEqual(sanctions.check(st, B)['state'], 'listed')
        r = sanctions.check(st, A)
        self.assertEqual(r['state'], 'not_listed')
        self.assertIn('not proof', r['detail'])
        self.assertEqual(sanctions.check(st, B.upper().replace('0X', '0x'))['state'], 'listed')  # normalization

    def test_stale_logic(self):
        from dappman import sanctions
        st = self._store_with_fixture(fetched=1)
        self.assertTrue(sanctions.stale(st))                                  # fetched_at=1 is ancient
        st.set_setting('ofac_sdn_fetched_at', 9999999999)
        self.assertFalse(sanctions.stale(st))

    def test_scan_alerts_on_sanctioned_counterparty(self):
        st = self._store_with_fixture()
        tid = st.add_target('wallet', '1', A, 'w')
        t = [x for x in st.targets() if x['id'] == tid][0]

        class F:
            def call(self, tool, a):
                if tool == 'get_native_balance': return '0'
                if tool == 'get_transactions':
                    return [{'hash': '0xh', 'blockNumber': '1', 'timeStamp': '1', 'from': B, 'to': A,
                             'value': '1', 'isError': '0'}]
                if tool in ('get_internal_transactions', 'get_token_transfers'): return []
                raise ApiError('unexpected ' + tool)

        scan(F(), st, t)                                       # baseline: no alerts
        al = scan(F(), st, t)['new_alerts']                    # same data -> nothing new
        self.assertEqual(al, [])
        f2 = F()
        orig = f2.call
        f2.call = lambda tool, a: ([{'hash': '0xh2', 'blockNumber': '2', 'timeStamp': '2', 'from': B, 'to': A,
                                     'value': '1', 'isError': '0'}] if tool == 'get_transactions' else orig(tool, a))
        codes = [c for _, c, _ in scan(f2, st, t)['new_alerts']]
        self.assertIn('sanctioned_counterparty', codes)
        a = [x for x in st.alerts() if x['code'] == 'sanctioned_counterparty'][0]
        lvl, msg = a['level'], a['msg']
        self.assertEqual(lvl, 'critical')
        self.assertIn('EVIL ACTOR', msg); self.assertIn('01/02/2026', msg)
        self.assertIn('not a legal conclusion', msg)


class ReputationT(unittest.TestCase):
    def test_flag_mapping_and_sources(self):
        from dappman import reputation
        payload = {'code': 1, 'result': {'phishing_activities': '1', 'sanctioned': '0', 'mixer': '0',
                                         'number_of_malicious_contracts_created': '3', 'data_source': 'SlowMist'}}

        class R:
            def read(self): return json.dumps(payload).encode()

        r = reputation.check(A, '1', opener=lambda *a, **k: R())
        self.assertTrue(r['ok'])
        self.assertIn('phishing activity', r['flags'])
        self.assertIn('created 3 malicious contract(s) per GoPlus sources', r['flags'])
        self.assertNotIn('mixer involvement', r['flags'])
        self.assertEqual(r['data_source'], 'SlowMist')
        self.assertIn('not a verdict', r['note'])

    def test_failure_is_unavailable_not_clean(self):
        from dappman import reputation
        def boom(*a, **k): raise OSError('dns fail')
        r = reputation.check(A, '1', opener=boom)
        self.assertFalse(r['ok']); self.assertIn('unavailable', r['error'])

    def test_api_error_code(self):
        from dappman import reputation
        class R:
            def read(self): return json.dumps({'code': 0, 'message': 'unsupported chain'}).encode()
        r = reputation.check(A, '999', opener=lambda *a, **k: R())
        self.assertFalse(r['ok']); self.assertIn('unsupported chain', r['error'])


class McpServerT(unittest.TestCase):
    def _tools(self, st):
        from dappman.mcpserver import make_tools
        return make_tools(lambda: st, lambda: None)

    def test_initialize_and_list(self):
        from dappman.mcpserver import handle
        tools = self._tools(Store(':memory:'))
        r = handle(tools, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}})
        self.assertEqual(r['result']['serverInfo']['name'], 'dappman-local')
        r = handle(tools, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        names = [t['name'] for t in r['result']['tools']]
        for n in ('dappman_list_targets', 'dappman_get_transactions', 'dappman_get_alerts',
                  'dappman_get_nft_inventory', 'dappman_check_address', 'dappman_get_balance',
                  'dappman_tx_status', 'dappman_gas'):
            self.assertIn(n, names)

    def test_notifications_get_no_response(self):
        from dappman.mcpserver import handle
        self.assertIsNone(handle({}, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}))

    def test_unknown_tool_and_method_rejected(self):
        from dappman.mcpserver import handle
        r = handle({}, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'evil_send', 'arguments': {}}})
        self.assertEqual(r['error']['code'], -32601)
        r = handle({}, {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/read'})
        self.assertEqual(r['error']['code'], -32601)

    def test_tool_call_reads_store(self):
        from dappman.mcpserver import handle
        st = Store(':memory:')
        st.add_target('wallet', '1', A, 'mywallet')
        tools = self._tools(st)
        r = handle(tools, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                           'params': {'name': 'dappman_list_targets', 'arguments': {}}})
        data = json.loads(r['result']['content'][0]['text'])
        self.assertEqual(data['targets'][0]['label'], 'mywallet')

    def test_stdio_roundtrip_ndjson(self):
        from dappman.mcpserver import make_tools, serve_stdio
        inp = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\nnot json\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        out = io.StringIO()
        serve_stdio(self._tools(Store(':memory:')), inp=inp, out=out)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)                       # notification produced no response
        self.assertEqual(json.loads(lines[0])['id'], 1)
        self.assertEqual(json.loads(lines[1])['error']['code'], -32700)

    def test_readonly_store_blocks_writes(self):
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        Store(db).add_target('wallet', '1', A, 'w')
        ro = Store(db, readonly=True)
        self.assertEqual(len(ro.targets()), 1)
        with self.assertRaises(Exception):
            ro.add_target('wallet', '56', B, 'x')             # sqlite mode=ro refuses


class CliT(unittest.TestCase):
    def test_check_address_json_and_history_flow(self):
        import contextlib
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(['--db', db, 'check-address', A, '--json', '--no-refresh'], client=HistFake())
        res = json.loads(buf.getvalue())
        self.assertEqual(res['ofac_sdn']['state'], 'unknown')  # no list cached, offline fake -> unknown, never clean
        self.assertIn('goplus', res)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(['--db', db, 'history', A], client=HistFake()), 2)  # not watched

    def test_nfts_command_derives_with_disclaimer(self):
        import contextlib
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(['--db', db, 'nfts', A, '--chains', '1'], client=HistFake())
        s = buf.getvalue()
        self.assertIn('DERIVED candidate inventory, NOT confirmed', s)
        # HistFake gives one erc721 row per block all TO A from B: 100 candidate items
        self.assertIn('100 candidate NFT item(s)', s)


if __name__ == '__main__':
    unittest.main()


class HardeningT(unittest.TestCase):
    def test_stale_ofac_cache_is_unknown_not_not_listed(self):
        from dappman import sanctions
        st = SanctionsT()._store_with_fixture(fetched=1)          # ancient
        r = sanctions.check(st, A)
        self.assertEqual(r['state'], 'unknown'); self.assertTrue(r['stale']); self.assertIn('freshness', r['detail'])
        r = sanctions.check(st, B)
        self.assertEqual(r['state'], 'listed'); self.assertIn('OLD', r['detail']); self.assertIn('network_note', r)
        import time as t
        st.set_setting('ofac_sdn_fetched_at', int(t.time()))
        self.assertEqual(sanctions.check(st, A)['state'], 'not_listed')

    def test_mcp_input_validation(self):
        from dappman.mcpserver import handle, make_tools
        tools = make_tools(lambda: Store(':memory:'), lambda: None)
        def call(name, args):
            r = handle(tools, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            return r
        for name, args in (('dappman_check_address', {'address': "0x1'; DROP TABLE txs;--"}),
                           ('dappman_get_balance', {'address': A, 'chain': '1/../../x'}),
                           ('dappman_tx_status', {'hash': '0x12'}),
                           ('dappman_get_transactions', {'limit': 'abc'}),
                           ('dappman_get_transactions', {'kinds': 'normal'})):
            r = call(name, args)
            self.assertTrue(r['result'].get('isError'), name)
        self.assertEqual(call('dappman_list_targets', 'x')['error']['code'], -32602)
        self.assertEqual(call(['x'], {})['error']['code'], -32601)
        lst = handle(tools, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in lst))
        names = {t['name'] for t in lst}
        self.assertTrue(all(n.startswith('dappman_') for n in names))
        self.assertFalse([n for n in names if any(w in n for w in ('sql', 'exec', 'fetch', 'url', 'write', 'send'))])

    def test_mcp_limit_is_capped(self):
        from dappman.mcpserver import _limit
        self.assertEqual(_limit(10**9), 200); self.assertEqual(_limit(0), 1)

    def test_nft_more_out_than_in_flagged_and_not_listed(self):
        from dappman.nfts import derive
        st = Store(':memory:'); tid = st.add_target('wallet', '1', A, 'w'); C = '0x' + 'ee' * 20
        st.add_txs(tid, [_tx({'hash': '0x1', 'blockNumber': '1', 'timeStamp': '1', 'from': A, 'to': B, 'contractAddress': C,
                              'tokenID': '3', 'tokenValue': '4'}, 'erc1155')])   # out with no matching in
        d = derive(st, tid, A)
        self.assertEqual(d['items'], []); self.assertEqual(d['inconsistent_tokens'], 1)
        self.assertIn('not current ownership', d['window'])

    def test_dashboard_page_has_new_legend_and_wrap(self):
        from dappman.web import PAGE
        self.assertIn('تحليل أسماء الدوال للاستدعاءات الداخلية', PAGE)
        self.assertNotIn('لا تُرى الاستدعاءات عبر multisig/router.', PAGE)
        self.assertIn('overflow-wrap:anywhere', PAGE)
