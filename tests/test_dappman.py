import json, os, tempfile, threading, unittest, urllib.request
from decimal import Decimal
from dappman.client import ApiError, McpClient
from dappman.store import Store
from dappman.monitor import scan, norm_addr
from dappman.web import serve, state
from dappman import cli

A = '0x' + 'ab' * 20


class Fake:
    def __init__(self):
        self.txs = [{'hash': '0x1', 'blockNumber': '10', 'timeStamp': '1700000000', 'from': '0x' + '11' * 20,
                     'to': A, 'value': '0', 'isError': '0', 'functionName': 'transfer(address,uint256)'}]
        self.src = 'contract X {}'; self.impl = ''; self.fail = set(); self.tt = []

    def call(self, tool, args):
        if tool in self.fail: raise ApiError('boom ' + tool)
        if tool == 'get_native_balance': return '2000000000000000000'
        if tool == 'get_contract_creation': return [{'contractCreator': '0x' + '22' * 20, 'timestamp': '1600000000', 'txHash': '0xc'}]
        if tool == 'get_contract_source':
            return [{'SourceCode': self.src, 'ContractName': 'X', 'CompilerVersion': 'v0.8', 'Proxy': '1' if self.impl else '0', 'Implementation': self.impl}]
        if tool == 'get_transactions': return list(self.txs)
        if tool == 'get_token_transfers': return list(self.tt)
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


if __name__ == '__main__':
    unittest.main()
