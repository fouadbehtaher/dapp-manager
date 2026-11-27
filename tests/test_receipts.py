"""Receipt-derived identity: decoding (721 / 1155 single / 1155 batch), reconciliation, endpoint policy, reorg handling, CLI e2e."""
import io, json, unittest, urllib.error
from contextlib import redirect_stdout
from unittest import mock
from dappman import cli, receipts
from dappman.receipts import ReceiptSource, parse_logs, assess, verify, T_TRANSFER, T_SINGLE, T_BATCH
from dappman.store import Store
from dappman.nfts import derive

ME = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40
NFT = '0x' + 'c' * 40
H = '0x' + '1' * 64
BH = '0x' + '2' * 64
BH2 = '0x' + '3' * 64


def t32(a): return '0x' + a[2:].rjust(64, '0')
def w(n): return hex(n)[2:].rjust(64, '0')


def log721(li, frm, to, tid, bh=BH, h=H):
    return {'address': NFT, 'topics': [T_TRANSFER, t32(frm), t32(to), '0x' + w(tid)], 'data': '0x', 'logIndex': hex(li),
            'blockHash': bh, 'transactionHash': h, 'removed': False}


def log1155_single(li, frm, to, tid, val, bh=BH, h=H):
    return {'address': NFT, 'topics': [T_SINGLE, t32(OTHER), t32(frm), t32(to)], 'data': '0x' + w(tid) + w(val), 'logIndex': hex(li),
            'blockHash': bh, 'transactionHash': h}


def log1155_batch(li, frm, to, ids, vals, bh=BH, h=H):
    n = len(ids)
    data = '0x' + w(64) + w(64 + 32 * (n + 1)) + w(n) + ''.join(w(i) for i in ids) + w(n) + ''.join(w(v) for v in vals)
    return {'address': NFT, 'topics': [T_BATCH, t32(OTHER), t32(frm), t32(to)], 'data': data, 'logIndex': hex(li), 'blockHash': bh, 'transactionHash': h}


def receipt(logs, block=10, bh=BH, h=H, status='0x1'):
    return {'transactionHash': h, 'blockNumber': hex(block), 'blockHash': bh, 'status': status, 'logs': logs}


class Net:
    """Fake JSON-RPC opener. routes: {host: callable(method, params) -> result | ('http', code) | ('raise', exc)}"""
    def __init__(self, routes): self.routes, self.hits = routes, []
    def __call__(self, req, timeout=0):
        host = req.full_url.split('//')[1]
        body = json.loads(req.data.decode())
        self.hits.append((host, body['method']))
        r = self.routes[host](body['method'], body['params'])
        if isinstance(r, tuple) and r[0] == 'http':
            raise urllib.error.HTTPError(req.full_url, r[1], 'x', {}, None)
        if isinstance(r, tuple) and r[0] == 'raise':
            raise r[1]
        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': r}).encode())


def node(receipts_by_hash, tip=1000, chain='0x1'):
    def f(m, p):
        if m == 'eth_chainId': return chain
        if m == 'eth_blockNumber': return hex(tip)
        if m == 'eth_getTransactionReceipt': return receipts_by_hash.get(p[0])
    return f


def mk_store(provider_rows, kind='erc721'):
    s = Store(':memory:'); t = s.add_target('wallet', '1', ME, 'w')
    rows = [{'hash': H, 'block': 10, 'ts': 1, 'frm': r[0], 'to': r[1], 'value': '1', 'failed': 0, 'kind': kind, 'token': 'APE', 'contract': NFT, 'token_id': str(r[2])} for r in provider_rows]
    s.add_txs(t, rows)
    return s, t


def src(routes, urls=None, **k):
    n = Net(routes)
    return ReceiptSource('1', urls=urls or ['https://' + h for h in routes], opener=n, delay=0, **k), n


class Decode(unittest.TestCase):
    def test_721_single_batch(self):
        ev = parse_logs([log721(3, OTHER, ME, 7), log1155_single(4, OTHER, ME, 9, 5), log1155_batch(5, ME, OTHER, [1, 2, 2], [10, 20, 30])])
        self.assertEqual([(e['kind'], e['token_id'], e['value'], e['log_index'], e['sub']) for e in ev],
                         [('erc721', '7', '1', 3, 0), ('erc1155', '9', '5', 4, 0), ('erc1155', '1', '10', 5, 0), ('erc1155', '2', '20', 5, 1), ('erc1155', '2', '30', 5, 2)])
        self.assertEqual(ev[0]['frm'], OTHER) and self.assertEqual(ev[0]['to'], ME)

    def test_erc20_removed_and_foreign_topics_ignored(self):
        erc20 = {'address': NFT, 'topics': [T_TRANSFER, t32(OTHER), t32(ME)], 'data': '0x' + w(5), 'logIndex': '0x1', 'blockHash': BH, 'transactionHash': H}
        removed = {**log721(2, OTHER, ME, 1), 'removed': True}
        self.assertEqual(parse_logs([erc20, removed, {'topics': ['0xdead'], 'logIndex': '0x0'}]), [])

    def test_malformed_logs_raise(self):
        bad_single = {**log1155_single(1, OTHER, ME, 1, 1), 'data': '0x1234'}
        bad_batch = log1155_batch(1, OTHER, ME, [1, 2], [1, 2]); bad_batch['data'] = bad_batch['data'][:-64]
        for lg in (bad_single, bad_batch, {**log721(1, OTHER, ME, 1), 'logIndex': 'zz'}):
            with self.assertRaises(ValueError): parse_logs([lg])


class Assess(unittest.TestCase):
    def test_states(self):
        ok = receipt([log721(1, OTHER, ME, 7)])
        self.assertEqual(assess(ok, H, 10, 1000)[0], 'verified')
        self.assertEqual(assess(ok, H, 10, 15)[0], 'pending')
        self.assertEqual(assess(ok, H, 10, None)[0], 'unknown')
        self.assertEqual(assess(ok, H, 11, 1000)[0], 'inconsistent')                       # stored block contradicts the receipt
        self.assertEqual(assess(ok, '0x' + '9' * 64, 10, 1000)[0], 'inconsistent')         # wrong tx
        self.assertEqual(assess(receipt([log721(1, OTHER, ME, 7, bh=BH2)]), H, 10, 1000)[0], 'inconsistent')  # log from another block
        self.assertEqual(assess({'junk': 1}, H, 10, 1000)[0], 'unknown')
        self.assertEqual(assess(receipt([], status='0x0'), H, 10, 1000)[0], 'verified')


class Reconcile(unittest.TestCase):
    def test_twins_resolved_and_inventory_from_receipt(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])          # provider returned two byte-identical rows
        self.assertEqual(s.ambiguous_rows(t), 2)
        r, n = src({'a.test': node({H: receipt([log721(3, OTHER, ME, 7), log721(9, OTHER, ME, 7)])})})
        rep = verify(s, t, ME, '1', r, s.ambiguous_hashes(t))
        self.assertEqual(rep['states'], {'verified': 1}); self.assertEqual(rep['provider_mismatch'], [])
        self.assertEqual(s.ambiguous_rows(t), 0)
        self.assertEqual(s.row_identity(t)[H], 'receipt_verified')
        self.assertEqual(derive(s, t, ME)['items'][0]['qty'], 2)
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM receipt_events').fetchone()[0], 2)

    def test_provider_omission_is_exposed_not_hidden(self):
        s, t = mk_store([(OTHER, ME, 7)])
        r, _ = src({'a.test': node({H: receipt([log721(3, OTHER, ME, 7), log721(9, OTHER, ME, 7)])})})
        rep = verify(s, t, ME, '1', r, [H])
        self.assertEqual(rep['provider_mismatch'][0]['only_receipt'], 1)
        self.assertEqual(derive(s, t, ME)['items'][0]['qty'], 2)       # receipt wins for inventory
        self.assertEqual(s.receipt_summary(t)['provider_mismatch_txs'], 1)

    def test_provider_extra_row_is_flagged_and_receipt_count_used(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, _ = src({'a.test': node({H: receipt([log721(3, OTHER, ME, 7)])})})
        rep = verify(s, t, ME, '1', r, [H])
        self.assertEqual(rep['provider_mismatch'][0]['only_provider'], 1)
        self.assertEqual(derive(s, t, ME)['items'][0]['qty'], 1)        # not forced to match the provider's 2

    def test_events_not_involving_wallet_are_not_stored(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, _ = src({'a.test': node({H: receipt([log721(3, OTHER, ME, 7), log721(4, OTHER, ME, 7), log721(5, OTHER, '0x' + 'd' * 40, 8)])})})
        verify(s, t, ME, '1', r, [H])
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM receipt_events').fetchone()[0], 2)

    def test_1155_batch_receipt_inventory(self):
        s, t = mk_store([(OTHER, ME, 1)], kind='erc1155')
        r, _ = src({'a.test': node({H: receipt([log1155_batch(2, OTHER, ME, [1, 2], [3, 4])])})})
        verify(s, t, ME, '1', r, [H])
        inv = {i['token_id']: i['qty'] for i in derive(s, t, ME)['items']}
        self.assertEqual(inv, {'1': 3, '2': 4})

    def test_non_nft_twins_stay_ambiguous(self):
        s = Store(':memory:'); t = s.add_target('wallet', '1', ME, 'w')
        rows = [{'hash': H, 'block': 10, 'ts': 1, 'frm': OTHER, 'to': ME, 'value': '5', 'failed': 0, 'kind': 'erc20', 'token': 'U|6', 'contract': NFT, 'token_id': ''}] * 2
        s.add_txs(t, rows)
        s.save_receipt(t, H, '1', 10, BH, 'verified', '', 'h', 1000, [])
        self.assertEqual(s.ambiguous_rows(t), 2)      # receipts do not cover erc20 twins


class Policy(unittest.TestCase):
    def test_null_then_second_endpoint_answers(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, n = src({'a.test': node({}), 'b.test': node({H: receipt([log721(3, OTHER, ME, 7), log721(4, OTHER, ME, 7)])})})
        rep = verify(s, t, ME, '1', r, [H])
        self.assertEqual(rep['states'], {'verified': 1}); self.assertEqual(rep['host'], 'b.test')
        self.assertIn(('a.test', 'eth_getTransactionReceipt'), n.hits)

    def test_all_null_is_unknown_and_stays_ambiguous(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, _ = src({'a.test': node({}), 'b.test': node({})})
        rep = verify(s, t, ME, '1', r, [H])
        self.assertEqual(rep['states'], {'unknown': 1}); self.assertEqual(s.ambiguous_rows(t), 2)
        self.assertIn('null', s.receipt_states(t)[H]['reason'])

    def test_429_stops_run_and_never_rotates(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, n = src({'a.test': lambda m, p: ('http', 429) if m == 'eth_getTransactionReceipt' else node({})(m, p), 'b.test': node({H: receipt([])})})
        rep = verify(s, t, ME, '1', r, [H, '0x' + '5' * 64])
        self.assertEqual(rep['checked'], 1); self.assertTrue(rep['stopped'] and '429' in rep['stopped'])
        self.assertNotIn(('b.test', 'eth_getTransactionReceipt'), n.hits)
        self.assertEqual(s.receipt_states(t)[H]['state'], 'unknown')

    def test_401_and_403_also_stop(self):
        for code in (401, 403):
            s, t = mk_store([(OTHER, ME, 7)])
            r, n = src({'a.test': lambda m, p, c=code: ('http', c) if m == 'eth_getTransactionReceipt' else node({})(m, p), 'b.test': node({H: receipt([])})})
            verify(s, t, ME, '1', r, [H]); self.assertTrue(r.stopped); self.assertNotIn(('b.test', 'eth_getTransactionReceipt'), n.hits)

    def test_wrong_chain_endpoint_is_skipped(self):
        s, t = mk_store([(OTHER, ME, 7)])
        r, n = src({'a.test': node({H: receipt([])}, chain='0x89'), 'b.test': node({H: receipt([log721(1, OTHER, ME, 7)])})})
        rep = verify(s, t, ME, '1', r, [H]); self.assertEqual(rep['host'], 'b.test')

    def test_network_error_falls_through_and_garbage_is_unknown(self):
        s, t = mk_store([(OTHER, ME, 7)])
        r, _ = src({'a.test': lambda m, p: ('raise', OSError('boom \x1b[31m' + 'x' * 500)) if m == 'eth_getTransactionReceipt' else node({})(m, p),
                    'b.test': node({H: 'not a dict'})})
        rep = verify(s, t, ME, '1', r, [H])
        self.assertEqual(rep['states'], {'unknown': 1})
        self.assertTrue(len(s.receipt_states(t)[H]['reason']) <= 160 and '\x1b' not in s.receipt_states(t)[H]['reason'])

    def test_pending_and_inconsistent_do_not_clear_ambiguity(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        r, _ = src({'a.test': node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])}, tip=12)})
        verify(s, t, ME, '1', r, [H]); self.assertEqual(s.receipt_states(t)[H]['state'], 'pending'); self.assertEqual(s.ambiguous_rows(t), 2)
        r, _ = src({'a.test': node({H: receipt([log721(1, OTHER, ME, 7)], block=11)})})
        verify(s, t, ME, '1', r, [H]); self.assertEqual(s.receipt_states(t)[H]['state'], 'inconsistent'); self.assertEqual(s.ambiguous_rows(t), 2)


class Reorg(unittest.TestCase):
    def test_block_hash_change_marks_reorg_suspected_then_reverifies(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        ev = lambda bh: node({H: receipt([log721(1, OTHER, ME, 7, bh=bh), log721(2, OTHER, ME, 7, bh=bh)], bh=bh)})
        verify(s, t, ME, '1', src({'a.test': ev(BH)})[0], [H]); self.assertEqual(s.ambiguous_rows(t), 0)
        verify(s, t, ME, '1', src({'a.test': ev(BH2)})[0], [H])
        self.assertEqual(s.receipt_states(t)[H]['state'], 'reorg_suspected'); self.assertEqual(s.ambiguous_rows(t), 2)
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM receipt_events').fetchone()[0], 0)
        verify(s, t, ME, '1', src({'a.test': ev(BH2)})[0], [H]); self.assertEqual(s.receipt_states(t)[H]['state'], 'verified')

    def test_transient_failure_does_not_downgrade_and_reverify_is_idempotent(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        good = node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])})
        verify(s, t, ME, '1', src({'a.test': good})[0], [H]); verify(s, t, ME, '1', src({'a.test': good})[0], [H])
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM receipt_events').fetchone()[0], 2)
        verify(s, t, ME, '1', src({'a.test': node({})})[0], [H])
        self.assertEqual(s.receipt_states(t)[H]['state'], 'verified')

    def test_failed_recheck_is_visible_beside_historical_verified(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        good = node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])})
        verify(s, t, ME, '1', src({'a.test': good})[0], [H])
        self.assertEqual(s.stale_checks(t), {}); self.assertEqual(s.receipt_summary(t)['last_check_failed_txs'], 0)
        before = s.receipt_states(t)[H]
        s.db.execute('UPDATE tx_receipts SET ts=1000 WHERE hash=?', (H,)); s.db.commit()
        verify(s, t, ME, '1', src({'a.test': node({})})[0], [H])
        row = s.receipt_states(t)[H]
        self.assertEqual(row['state'], 'verified'); self.assertEqual(row['ts'], 1000)      # historical result and its time are NOT refreshed
        self.assertEqual(row['last_state'], 'unknown'); self.assertTrue(row['last_ts'] > 1000)
        sm = s.receipt_summary(t)
        self.assertEqual(sm['last_check_failed_txs'], 1); self.assertEqual(sm['oldest_verified_ts'], 1000); self.assertIn('null', sm['last_check_reason'])
        self.assertEqual(s.row_identity(t)[H], 'receipt_verified'); self.assertEqual(s.ambiguous_rows(t), 0)
        verify(s, t, ME, '1', src({'a.test': good})[0], [H])          # a later success clears the failed-check marker
        self.assertEqual(s.stale_checks(t), {})

    def test_failed_recheck_shown_in_history_json_and_old_db_migrates(self):
        import sqlite3, tempfile, os
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        good = node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])})
        verify(s, t, ME, '1', src({'a.test': good})[0], [H]); verify(s, t, ME, '1', src({'a.test': node({})})[0], [H])
        out = io.StringIO()
        with redirect_stdout(out): cli._cmd_history(mock.Mock(address=ME, chain='1', categories='erc721', limit=10, json=True), s, None)
        d = json.loads(out.getvalue())
        self.assertTrue(all(r['identity'] == 'receipt_verified' and r['last_check']['last_state'] == 'unknown' for r in d['rows']))
        out = io.StringIO()
        with redirect_stdout(out): cli._cmd_history(mock.Mock(address=ME, chain='1', categories='erc721', limit=10, json=False), s, None)
        self.assertIn('LAST CHECK FAILED', out.getvalue())
        # a v0.7 database created before the last-check columns existed gains them without losing rows
        d_ = tempfile.mkdtemp(); p = os.path.join(d_, 'o.db')
        c = sqlite3.connect(p)
        c.executescript('CREATE TABLE tx_receipts(target_id INT, hash TEXT, chain TEXT, block INT, block_hash TEXT, state TEXT, reason TEXT, host TEXT, tip INT, ts INT, n_events INT DEFAULT 0, only_receipt INT DEFAULT 0, only_provider INT DEFAULT 0, PRIMARY KEY(target_id,hash));')
        c.execute("INSERT INTO tx_receipts VALUES(1,'0xa','1',1,'0xb','verified','','h',1,5,0,0,0)"); c.commit(); c.close()
        s2 = Store(p)
        self.assertEqual(s2.receipt_states(1)['0xa']['state'], 'verified'); self.assertEqual(s2.receipt_states(1)['0xa']['last_state'], '')

    def test_new_verified_events_invalidate_saved_confirmation(self):
        s, t = mk_store([(OTHER, ME, 7), (OTHER, ME, 7)])
        s.save_confirmations(t, {'results': [{'contract': NFT, 'token_id': '7', 'standard': 'erc721', 'state': 'confirmed_owner', 'block': 5, 'rpc': 'h'}]})
        verify(s, t, ME, '1', src({'a.test': node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])})})[0], [H])
        self.assertEqual(s.confirmations(t), {})


class CliE2E(unittest.TestCase):
    def test_verify_identity_and_history_provenance(self):
        import tempfile, os
        db = os.path.join(tempfile.mkdtemp(), 'r.db'); s = Store(db); t = s.add_target('wallet', '1', ME, 'w')
        s.add_txs(t, [{'hash': H, 'block': 10, 'ts': 1, 'frm': OTHER, 'to': ME, 'value': '1', 'failed': 0, 'kind': 'erc721', 'token': 'APE', 'contract': NFT, 'token_id': '7'}] * 2)
        s.db.close()
        n = Net({'ethereum-rpc.publicnode.com': node({H: receipt([log721(1, OTHER, ME, 7), log721(2, OTHER, ME, 7)])})})
        with mock.patch('urllib.request.urlopen', n), mock.patch('dappman.receipts.time.sleep'):
            buf = io.StringIO()
            with redirect_stdout(buf): self.assertEqual(cli.main(['--db', db, 'verify-identity', ME, '--json']), 0)
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep['states'], {'verified': 1}); self.assertEqual(rep['identity_ambiguous_rows'], 0)
            self.assertIn('unproven', rep['completeness'])
            buf = io.StringIO()
            with redirect_stdout(buf): cli.main(['--db', db, 'history', ME, '--json'])
            h = json.loads(buf.getvalue())
            self.assertEqual({r['identity'] for r in h['rows']}, {'receipt_verified'}); self.assertEqual(h['receipts']['by_state'], {'verified': 1})
            buf = io.StringIO()
            with redirect_stdout(buf): cli.main(['--db', db, 'history', ME])
            self.assertIn('[receipt-verified]', buf.getvalue())

    def test_unwatched_address_is_refused(self):
        import tempfile, os
        db = os.path.join(tempfile.mkdtemp(), 'x.db')
        with redirect_stdout(io.StringIO()): self.assertEqual(cli.main(['--db', db, 'verify-identity', ME]), 2)


if __name__ == '__main__':
    unittest.main()
