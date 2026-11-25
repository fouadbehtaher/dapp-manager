import json, os, tempfile, threading, unittest, urllib.request
from decimal import Decimal
from dappman.client import ApiError, McpClient
from dappman.store import Store
from dappman.monitor import scan, norm_addr, profile, sensitive
from dappman.web import serve, state
from dappman.abi import keccak256


def inp(sig):
    return '0x' + keccak256(sig.encode())[:4].hex() + '00' * 32

from dappman import cli

A = '0x' + 'ab' * 20


class Fake:
    def __init__(self):
        self.txs = [{'hash': '0x1', 'blockNumber': '10', 'timeStamp': '1700000000', 'from': '0x' + '11' * 20,
                     'to': A, 'value': '0', 'isError': '0', 'functionName': 'transfer(address,uint256)',
                     'input': inp('transfer(address,uint256)'), 'methodId': inp('transfer(address,uint256)')[:10]}]
        self.src = 'contract X {}'; self.impl = ''; self.fail = set(); self.tt = []; self.internal = []

    def call(self, tool, args):
        if tool in self.fail: raise ApiError('boom ' + tool)
        if tool == 'get_native_balance': return '2000000000000000000'
        if tool == 'get_contract_creation': return [{'contractCreator': '0x' + '22' * 20, 'timestamp': '1600000000', 'txHash': '0xc'}]
        if tool == 'get_contract_source':
            return [{'SourceCode': self.src, 'ContractName': 'X', 'CompilerVersion': 'v0.8', 'Proxy': '1' if self.impl else '0', 'Implementation': self.impl}]
        if tool == 'get_transactions': return list(self.txs)
        if tool == 'get_token_transfers': return list(self.tt)
        if tool == 'get_internal_transactions': return list(self.internal)
        raise ApiError('unexpected ' + tool)


def mk(kind='contract'):
    st = Store(':memory:'); f = Fake()
    tid = st.add_target(kind, '1', A, 't')
    return st, f, st.targets()[0]


class T(unittest.TestCase):
    def test_baseline_has_no_alerts(self):
        st, f, t = mk(); r = scan(f, st, t)
        self.assertTrue(r['snapshot']['baseline']); self.assertEqual(r['new_alerts'], [])
        self.assertEqual(r['snapshot']['native_balance'], '2')

    def test_new_failed_large_tx(self):
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, {'hash': '0x2', 'blockNumber': '11', 'timeStamp': '1700000100', 'from': A, 'to': A,
                         'value': str(15 * 10**18), 'isError': '1'})
        codes = [c for _, c, _ in scan(f, st, t)['new_alerts']]
        self.assertIn('new_tx', codes); self.assertIn('failed_tx', codes); self.assertIn('large_value', codes)
        self.assertEqual(scan(f, st, t)['new_alerts'], [])  # no duplicate alert

    def test_source_and_proxy_change_critical(self):
        st, f, t = mk(); scan(f, st, t)
        f.src = 'contract Y {}'; f.impl = '0x' + '33' * 20
        al = scan(f, st, t)['new_alerts']
        crit = {c for l, c, _ in al if l == 'critical'}
        self.assertEqual(crit, {'source_changed', 'implementation_changed'})

    def test_unverified_flag(self):
        st, f, t = mk(); scan(f, st, t); f.src = ''
        self.assertIn('verification_changed', [c for _, c, _ in scan(f, st, t)['new_alerts']])

    def test_errors_never_become_zero(self):
        st, f, t = mk(); f.fail = {'get_native_balance', 'get_transactions'}
        s = scan(f, st, t)['snapshot']
        self.assertTrue(s['status']['balance'].startswith('error'))
        self.assertNotIn('native_balance', s)
        self.assertTrue(s['status']['transactions'].startswith('error'))

    def test_category_failure_alert(self):
        st, f, t = mk(); scan(f, st, t); f.fail = {'get_token_transfers'}
        self.assertIn('category_failed', [c for _, c, _ in scan(f, st, t)['new_alerts']])

    def test_wallet_skips_contract_calls(self):
        st, f, t = mk('wallet'); f.fail = {'get_contract_source', 'get_contract_creation'}
        s = scan(f, st, t)['snapshot']; self.assertNotIn('source', s['status'])

    def test_address_validation(self):
        with self.assertRaises(ValueError): norm_addr('0x123')
        self.assertEqual(norm_addr('0x' + 'AB' * 20), A)

    def test_client_refuses_non_read_tools(self):
        with self.assertRaises(ApiError): McpClient().call('render_etherscan_widget', {})
        with self.assertRaises(ApiError): McpClient().call('send_transaction', {})

    def test_cli_flow_offline(self):
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        self.assertEqual(cli.main(['--db', db, 'add', A, '--label', 'c'], client=Fake()), 0)
        cli.main(['--db', db, 'scan'], client=Fake()); cli.main(['--db', db, 'export', '--out', d + '/o'], client=Fake())
        self.assertTrue(os.path.exists(d + '/o/txs.csv'))
        self.assertTrue(cli.main(['--db', db, 'remove', A], client=Fake()) == 0)
        self.assertEqual(Store(db).targets(), [])

    def test_dashboard_http(self):
        st, f, t = mk(); scan(f, st, t)
        srv = serve(lambda: st, lambda: f, port=0); port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
        try:
            base = f'http://127.0.0.1:{port}'
            self.assertIn('dir="rtl"', urllib.request.urlopen(base + '/').read().decode())
            s = json.loads(urllib.request.urlopen(base + '/api/state').read())
            self.assertEqual(s['targets'][0]['snapshot']['name'], 'X')
            r = urllib.request.urlopen(urllib.request.Request(base + '/api/scan', method='POST', headers={'X-Dappman': '1'}))
            self.assertEqual(json.loads(r.read())['scanned'], 1)
            with self.assertRaises(Exception): urllib.request.urlopen(base + '/nope')
        finally:
            srv.shutdown()

    def _srv(self):
        st, f, t = mk(); scan(f, st, t)
        srv = serve(lambda: st, lambda: f, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f'http://127.0.0.1:{srv.server_address[1]}'

    def _post(self, base, headers):
        import urllib.error
        try:
            return urllib.request.urlopen(urllib.request.Request(base + '/api/scan', method='POST', headers=headers)).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_csrf_and_host_protection(self):
        srv, base = self._srv()
        try:
            self.assertEqual(self._post(base, {}), 403)                                   # no custom header (simple cross-site form)
            self.assertEqual(self._post(base, {'X-Dappman': '1', 'Origin': 'https://evil.example'}), 403)
            self.assertEqual(self._post(base, {'X-Dappman': '1', 'Host': 'evil.example'}), 403)  # DNS rebinding
            self.assertEqual(self._post(base, {'X-Dappman': '1', 'Origin': base}), 200)
            import urllib.error
            req = urllib.request.Request(base + '/api/state', headers={'Host': 'evil.example:80'})
            with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req)
        finally:
            srv.shutdown(); srv.server_close()

    def test_loopback_only_and_headers(self):
        with self.assertRaises(ValueError): serve(lambda: None, lambda: None, host='0.0.0.0', port=0)
        srv, base = self._srv()
        try:
            r = urllib.request.urlopen(base + '/')
            csp = r.headers['Content-Security-Policy']
            self.assertIn("default-src 'none'", csp); self.assertNotIn('unsafe-inline', csp)
            self.assertEqual(r.headers['X-Frame-Options'], 'DENY')
            body = r.read().decode(); nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
            self.assertIn(f'nonce="{nonce}"', body); self.assertNotIn('onclick=', body)
        finally:
            srv.shutdown(); srv.server_close()

    def test_hostile_token_metadata_stays_data(self):
        st, f, t = mk('wallet'); f.tt = [{'hash': '0x9', 'blockNumber': '1', 'timeStamp': '1700000000', 'from': A, 'to': A,
            'value': '5', 'tokenSymbol': '<img src=x onerror=alert(1)>' + 'A' * 100, 'tokenDecimal': '0'}]
        scan(f, st, t)
        data = state(st)
        sym = data['targets'][0]['recent'][0]['token']
        self.assertLessEqual(len(sym.split('|')[0]), 20)           # capped
        # page escapes every dynamic value via E() and uses textless JSON; CSP blocks inline handlers anyway
        from dappman.web import PAGE
        self.assertIn("const E=s=>", PAGE); self.assertNotIn('innerHTML=x', PAGE)

    def test_read_only_no_write_endpoints(self):
        srv, base = self._srv()
        try:
            for m in ('PUT', 'DELETE', 'PATCH'):
                import urllib.error
                try: urllib.request.urlopen(urllib.request.Request(base + '/api/scan', method=m)); code = 200
                except urllib.error.HTTPError as e: code = e.code
                self.assertIn(code, (501, 405))
        finally:
            srv.shutdown(); srv.server_close()

    def test_coverage_gap_detected_on_busy_contract(self):
        st, f, t = mk(); scan(f, st, t)
        n = [0]
        def busy(tool, args):
            if tool == 'get_transactions':
                rows = []
                for i in range(50):
                    n[0] += 1
                    rows.append({'hash': f'0xn{n[0]}', 'blockNumber': str(n[0]), 'timeStamp': '1700000000', 'from': A, 'to': A, 'value': '0', 'isError': '0'})
                return rows
            return Fake.call(f, tool, args)
        f.call = busy
        r = scan(f, st, t)
        self.assertIn('coverage_gap', [c for _, c, _ in r['new_alerts']])
        self.assertEqual(r['snapshot']['gaps'], ['transactions'])
        self.assertEqual(r['new_txs'], 250)  # paged to the cap, not silently 50

    def test_paging_stops_at_known_record(self):
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, {'hash': '0x2', 'blockNumber': '11', 'timeStamp': '1700000100', 'from': A, 'to': A, 'value': '0', 'isError': '0'})
        r = scan(f, st, t)
        self.assertEqual(r['snapshot']['gaps'], []); self.assertEqual(r['new_txs'], 1)

    def test_units_are_native_not_usd(self):
        from dappman.monitor import fmt
        self.assertEqual(fmt(Decimal('1234.5678901')), '1,234.56789'); self.assertEqual(fmt(0), '0'); self.assertEqual(fmt(Decimal('1e-9')), '<0.000001')
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, {'hash': '0x3', 'blockNumber': '12', 'timeStamp': '1700000200', 'from': A, 'to': A, 'value': str(12 * 10**18), 'isError': '0'})
        msg = [m for _, c, m in scan(f, st, t)['new_alerts'] if c == 'large_value'][0]
        self.assertIn('native coin units, not USD', msg); self.assertIn('12 ETH', msg)

    def test_sensitive_call_alert_and_profile(self):
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, {'hash': '0xs1', 'blockNumber': '20', 'timeStamp': '1700000300', 'from': '0x' + '44' * 20, 'to': A,
                         'value': '0', 'isError': '0', 'functionName': 'transferOwnership(address newOwner)', 'input': inp('transferOwnership(address)')})
        f.txs.insert(0, {'hash': '0xs2', 'blockNumber': '21', 'timeStamp': '1700000400', 'from': '0x' + '55' * 20, 'to': A,
                         'value': '0', 'isError': '1', 'functionName': 'upgradeTo(address)', 'input': inp('upgradeTo(address)')})
        al = scan(f, st, t)['new_alerts']
        sc = [(l, m) for l, c, m in al if c == 'sensitive_call']
        self.assertEqual(len(sc), 2)
        self.assertEqual(sorted(l for l, _ in sc), ['info', 'warn'])   # reverted call is only info; none is 'critical'
        self.assertTrue(any('no state change expected' in m for _, m in sc))
        self.assertTrue(any('not proof of an attack' in m for _, m in sc))
        p = profile(st.window(t['id']), A, verified=True)
        self.assertEqual(p['calls'], 3); self.assertEqual(p['failed'], 1); self.assertEqual(len(p['sensitive']), 2)

    def test_sensitive_matching_is_name_only(self):
        self.assertIsNone(sensitive('0xf2fde38b')); self.assertIsNone(sensitive('transfer'))
        self.assertEqual(sensitive('pause')[0], 'warn'); self.assertEqual(sensitive('upgradeToAndCall')[0], 'critical')

    def test_wallet_never_raises_sensitive(self):
        st, f, t = mk('wallet'); scan(f, st, t)
        f.txs.insert(0, {'hash': '0xw', 'blockNumber': '30', 'timeStamp': '1700000500', 'from': A, 'to': A, 'value': '0', 'isError': '0', 'functionName': 'transferOwnership(address)', 'input': inp('transferOwnership(address)')})
        self.assertNotIn('sensitive_call', [c for _, c, _ in scan(f, st, t)['new_alerts']])

    def test_dapp_grouping_and_v03_db_migration(self):
        import sqlite3
        d = tempfile.mkdtemp(); db = os.path.join(d, 'old.db')
        c = sqlite3.connect(db)  # a database exactly as v0.3 created it (no dapp column)
        c.execute("CREATE TABLE targets(id INTEGER PRIMARY KEY, kind TEXT, chain TEXT, address TEXT, label TEXT, UNIQUE(chain,address))")
        c.execute("INSERT INTO targets(kind,chain,address,label) VALUES('contract','1',?,'old')", (A,)); c.commit(); c.close()
        st = Store(db)
        self.assertEqual(st.targets()[0]['dapp'], '')
        st.add_target('contract', '1', A, 'old', 'MyDapp')
        self.assertEqual(st.targets()[0]['dapp'], 'MyDapp')

    def test_cli_dapp_and_profile(self):
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        cli.main(['--db', db, 'add', A, '--label', 'c', '--dapp', 'Swap'], client=Fake())
        cli.main(['--db', db, 'scan'], client=Fake())
        self.assertEqual(cli.main(['--db', db, 'profile', '--dapp', 'Swap'], client=Fake()), 0)

    def test_watch_survives_one_bad_target(self):
        class Boom(Fake):
            def call(self, tool, args): raise RuntimeError('network down')
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        cli.main(['--db', db, 'add', A], client=Fake())
        self.assertEqual(cli.main(['--db', db, 'scan'], client=Boom()), 0)  # unexpected error must not crash the run

    def _own_tx(self, h, frm, to, name='transferOwnership(address)', sig='transferOwnership(address)', failed='0'):
        return {'hash': h, 'blockNumber': '40', 'timeStamp': '1700000600', 'from': frm, 'to': to, 'value': '0',
                'isError': failed, 'functionName': name, 'input': inp(sig)}

    def _codes(self, f, st, t):
        return [c for _, c, _ in scan(f, st, t)['new_alerts']]

    def test_only_incoming_direct_tx_to_watched_contract(self):
        other = '0x' + '77' * 20
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, self._own_tx('0xo1', A, other))        # OUTGOING from the contract to someone else
        f.txs.insert(0, self._own_tx('0xo2', other, other))    # unrelated to/from
        f.txs.insert(0, self._own_tx('0xo3', other, ''))       # contract creation style (empty to)
        self.assertNotIn('sensitive_call', self._codes(f, st, t))

    def test_internal_rows_are_data_only_and_cannot_alert_sensitive(self):
        # v0.5: internal transactions ARE collected as data (visibility into routed calls), but internal rows
        # carry no function name/selector, so they can never raise a sensitive_call alert.
        calls = []
        st, f, t = mk(); orig = f.call
        f.call = lambda tool, a: (calls.append(tool), orig(tool, a))[1]
        scan(f, st, t); self.assertIn('get_internal_transactions', calls)
        f.internal = [{'hash': '0xi1', 'blockNumber': '50', 'timeStamp': '1700000700', 'from': '0x' + '88' * 20,
                       'to': A, 'value': str(20 * 10**18), 'isError': '0'}]  # no functionName/methodId keys
        codes = [c for _, c, _ in scan(f, st, t)['new_alerts']]
        self.assertIn('new_internal_txs', codes); self.assertIn('large_value_internal', codes)
        self.assertNotIn('sensitive_call', codes)

    def test_forged_label_without_matching_selector_is_ignored(self):
        st, f, t = mk(); scan(f, st, t)
        f.txs.insert(0, self._own_tx('0xf1', '0x' + '66' * 20, A, sig='transfer(address,uint256)'))  # label says ownership, input says transfer
        self.assertNotIn('sensitive_call', self._codes(f, st, t))
        self.assertEqual(profile(st.window(t['id']), A, verified=True)['sensitive'], [])

    def test_missing_input_means_unverified_name(self):
        st, f, t = mk(); scan(f, st, t)
        r = self._own_tx('0xf2', '0x' + '66' * 20, A); del r['input']; r['methodId'] = '0xf2fde38b'
        f.txs.insert(0, r)
        self.assertNotIn('sensitive_call', self._codes(f, st, t))

    def test_unverified_contract_has_no_sensitive_alerts(self):
        st, f, t = mk(); f.src = ''; scan(f, st, t)
        f.txs.insert(0, self._own_tx('0xu1', '0x' + '66' * 20, A))
        self.assertNotIn('sensitive_call', self._codes(f, st, t))

    def test_keccak_known_vectors(self):
        from dappman.abi import selector_matches
        self.assertEqual(keccak256(b'').hex(), 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470')
        self.assertEqual(keccak256(b'transfer(address,uint256)')[:4].hex(), 'a9059cbb')
        self.assertEqual(keccak256(b'transferOwnership(address)')[:4].hex(), 'f2fde38b')
        self.assertTrue(selector_matches('approve(address _s, uint256 _v)', '0x095ea7b3' + '00' * 64))
        self.assertFalse(selector_matches('f((address,uint256))', '0x095ea7b3'))  # tuples unverifiable -> not trusted


H1 = '0x' + '1a' * 32


class TxFake:
    """Fake MCP for tx status. mode: success/failed/pending/not_found/error/emptyreceipt"""
    def __init__(self, mode='success'): self.mode = mode; self.gas = {'LastBlock': '5', 'SafeGasPrice': '1.0', 'ProposeGasPrice': '2.0', 'FastGasPrice': '3.0', 'suggestBaseFee': '0.9'}
    def call(self, tool, a):
        m = self.mode
        if m == 'error': raise ApiError('rate limited')
        if tool == 'get_transaction_by_hash':
            if m == 'not_found': return None
            return {'blockNumber': None if m == 'pending' else '0x10', 'from': A, 'to': A, 'nonce': '0x5', 'gasPrice': '0x3b9aca00'}
        if tool == 'get_transaction_receipt_status':
            return {'status': {'success': '1', 'failed': '0'}.get(m, '')}
        if tool == 'get_transaction_status':
            return {'isError': '1' if m == 'failed' else '0', 'errDescription': 'out of gas' if m == 'failed' else ''}  # note: isError 0 even for unknown hashes
        if tool == 'get_gas_oracle':
            if m == 'error': raise ApiError('x')
            return self.gas
        raise ApiError('unexpected ' + tool)


class TxT(unittest.TestCase):
    def st(self, mode):
        from dappman.txstatus import check
        return check(TxFake(mode), '1', H1)

    def test_five_distinct_states(self):
        self.assertEqual(self.st('success')['status'], 'success')
        f = self.st('failed'); self.assertEqual(f['status'], 'failed'); self.assertIn('out of gas', f['detail'])
        self.assertEqual(self.st('pending')['status'], 'pending')
        self.assertEqual(self.st('not_found')['status'], 'not_found')
        self.assertEqual(self.st('error')['status'], 'unavailable')

    def test_unknown_hash_is_never_success(self):
        self.assertNotEqual(self.st('not_found')['status'], 'success')   # get_transaction_status says isError=0 for unknown hashes

    def test_mined_with_empty_receipt_is_unavailable_not_success(self):
        r = self.st('emptyreceipt'); self.assertEqual(r['status'], 'unavailable'); self.assertIn('not as success', r['detail'])

    def test_hash_validation(self):
        from dappman.txstatus import norm_hash
        with self.assertRaises(ValueError): norm_hash('0x12')
        self.assertEqual(norm_hash('0x' + 'AB' * 32), '0x' + 'ab' * 32)

    def test_tracking_alerts_on_transition_and_stuck_once(self):
        from dappman.txstatus import tx_scan
        import time as _t
        st = Store(':memory:'); st.add_target('tx', '1', H1, 'order-77'); t = st.targets()[0]
        f = TxFake('pending')
        r = tx_scan(f, st, t); self.assertTrue(r['snapshot']['baseline']); self.assertEqual(r['new_alerts'], [])
        snap = st.last_snapshot(t['id'])[1]; snap['first_seen'] = int(_t.time()) - 3600; st.save_snapshot(t['id'], snap)
        a1 = tx_scan(f, st, t)['new_alerts']; self.assertEqual([c for _, c, _ in a1], ['tx_pending_long'])
        self.assertEqual(tx_scan(f, st, t)['new_alerts'], [])          # not repeated
        f.mode = 'success'
        a2 = tx_scan(f, st, t)['new_alerts']; self.assertEqual([c for _, c, _ in a2], ['tx_status_changed']); self.assertIn('pending -> success', a2[0][2])
        f.mode = 'failed'
        self.assertEqual(tx_scan(f, st, t)['new_alerts'][0][0], 'warn')

    def test_gas_sampling_and_threshold_cross_once(self):
        from dappman.txstatus import gas_scan
        st = Store(':memory:'); f = TxFake(); st.set_setting('gas_below_1', 1.5)
        gas_scan(f, st, ['1']); self.assertEqual(st.alerts(), [])             # 2.0 > 1.5
        f.gas['ProposeGasPrice'] = '1.0'; gas_scan(f, st, ['1'])
        self.assertEqual([a['code'] for a in st.alerts()], ['gas_below'])
        gas_scan(f, st, ['1']); self.assertEqual(len(st.alerts()), 1)           # still below, no repeat
        f.gas['ProposeGasPrice'] = '2.0'; gas_scan(f, st, ['1']); f.gas['ProposeGasPrice'] = '1.0'; gas_scan(f, st, ['1'])
        self.assertEqual(len(st.alerts()), 2)                                   # re-crossing alerts again
        self.assertEqual(len(st.gas_rows('1')), 5)

    def test_gas_error_is_reported_not_zero(self):
        from dappman.txstatus import gas_scan
        st = Store(':memory:'); r = gas_scan(TxFake('error'), st, ['1'])
        self.assertIsNone(r[0][1]); self.assertEqual(st.gas_rows('1'), [])

    def test_cli_exit_codes_and_json(self):
        import io, contextlib
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        for mode, code in (('success', 0), ('failed', 3), ('pending', 4), ('not_found', 5), ('error', 6)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--db', db, 'tx-status', H1], client=TxFake(mode)), code)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): cli.main(['--db', db, 'tx-status', H1, '--json'], client=TxFake('failed'))
        self.assertEqual(json.loads(buf.getvalue())['status'], 'failed')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): cli.main(['--db', db, 'gas', '--json'], client=TxFake())
        self.assertEqual(json.loads(buf.getvalue())['unit'], 'gwei')

    def test_cli_track_scan_remove_and_dashboard_state(self):
        import io, contextlib
        d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(['--db', db, 'track', H1, '--label', 'order-77'], client=TxFake('pending'))
            cli.main(['--db', db, 'scan'], client=TxFake('pending'))
            cli.main(['--db', db, 'scan'], client=TxFake('success'))
        s = state(Store(db))
        self.assertEqual(s['targets'][0]['snapshot']['status_cat'], 'success'); self.assertEqual(s['gas'][0]['n'], 2)
        self.assertIn('tx_status_changed', [a['code'] for a in s['alerts']])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(['--db', db, 'remove', H1], client=TxFake())
        self.assertEqual(Store(db).targets(), [])

    def test_alerts_json_stdout_clean(self):
        import io, contextlib
        db = os.path.join(tempfile.mkdtemp(), 'y.db'); buf = io.StringIO()
        with contextlib.redirect_stdout(buf): cli.main(['--db', db, 'alerts', '--json'], client=TxFake())
        self.assertIsInstance(json.loads(buf.getvalue()), (list, dict))

    def test_bad_hash_exits_cleanly(self):
        import subprocess, sys
        r = subprocess.run([sys.executable, '-m', 'dappman', '--db', ':memory:', 'tx-status', '0x12'], capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(r.returncode, 2); self.assertIn('invalid tx hash', r.stderr); self.assertNotIn('Traceback', r.stderr)

    def test_client_allowlist_new_tools_only_reads(self):
        from dappman.client import READ_TOOLS
        for t in ('get_transaction_receipt_status', 'get_transaction_by_hash', 'get_gas_oracle', 'get_transaction_status'):
            self.assertIn(t, READ_TOOLS)
        for bad in ('send_transaction', 'render_etherscan_widget', 'get_logs'):
            with self.assertRaises(ApiError): McpClient().call(bad, {})

    def test_client_proxy_payload_none_is_not_error(self):
        c = McpClient(sleep=0); c.sid = 'x'
        c._rpc = lambda m, p=None, i=None: {'result': {'content': [{'text': json.dumps({'ok': True, 'result': None})}]}}
        self.assertIsNone(c.call('get_transaction_by_hash', {'txhash': H1, 'chainid': '1'}))


if __name__ == '__main__':
    unittest.main()
