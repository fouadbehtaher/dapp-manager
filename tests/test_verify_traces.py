"""verify-traces: CLI wiring, state handling, endpoint refusal, no rotation."""
import io, json, os, tempfile, unittest, urllib.error
from contextlib import redirect_stdout
from unittest import mock
from dappman import cli
from dappman.store import Store

ME, C = '0x' + 'a' * 40, '0x' + 'c' * 40
H = '0x' + '1' * 64


def tr(ta, f, t, v, h=H, blk=10):
    return {'type': 'call', 'traceAddress': ta, 'transactionHash': h, 'blockNumber': blk,
            'action': {'from': f, 'to': t, 'value': hex(v), 'callType': 'call'}}


class Net:
    def __init__(self, trace=None, tip=1000, code=None):
        self.trace, self.tip, self.code, self.hosts = trace, tip, code, []

    def __call__(self, req, timeout=0):
        m = json.loads(req.data)['method']; self.hosts.append(req.full_url)
        if self.code and m == 'trace_transaction':
            raise urllib.error.HTTPError(req.full_url, self.code, 'x', {}, None)
        res = {'eth_chainId': '0x1', 'eth_blockNumber': hex(self.tip), 'trace_transaction': self.trace}[m]
        return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': res}).encode())


def mkdb(n=2):
    db = os.path.join(tempfile.mkdtemp(), 't.db'); s = Store(db); t = s.add_target('wallet', '1', ME, 'w')
    s.add_txs(t, [{'hash': H, 'block': 10, 'ts': 1, 'frm': C, 'to': ME, 'value': '5', 'failed': 0, 'kind': 'internal'}] * n)
    s.db.close(); return db


def run(db, net, *extra):
    buf = io.StringIO()
    with mock.patch('urllib.request.urlopen', net), mock.patch('dappman.receipts.time.sleep'), redirect_stdout(buf):
        rc = cli.main(['--db', db, 'verify-traces', ME, '--json', *extra])
    return rc, json.loads(buf.getvalue())


class T(unittest.TestCase):
    def test_match_clears_ambiguity(self):
        db = mkdb(); self.assertEqual(Store(db).ambiguous_rows(1), 2)
        rc, rep = run(db, Net([tr([], C, ME, 0), tr([0], C, ME, 5), tr([1], C, ME, 5)]))
        self.assertEqual((rc, rep['states'], rep['identity_ambiguous_rows']), (0, {'match': 1}, 0))
        self.assertIn('unproven', rep['completeness'])

    def test_provider_collapsed_stays_ambiguous(self):
        db = mkdb(2)
        _, rep = run(db, Net([tr([0], C, ME, 5), tr([1], C, ME, 5), tr([2], C, ME, 5)]))
        self.assertEqual((rep['states'], rep['identity_ambiguous_rows']), ({'trace_has_more': 1}, 2))

    def test_pending_and_inconsistent(self):
        _, rep = run(mkdb(), Net([tr([0], C, ME, 5), tr([1], C, ME, 5)], tip=15))
        self.assertEqual(rep['states'], {'pending': 1})
        _, rep = run(mkdb(), Net([tr([0], C, ME, 5, blk=99)]))
        self.assertEqual(rep['states'], {'inconsistent': 1})
        _, rep = run(mkdb(), Net([tr([0], C, ME, 5, h='0x' + '2' * 64)]))
        self.assertEqual(rep['states'], {'inconsistent': 1})

    def test_refusal_stops_without_rotation(self):
        db = mkdb(); net = Net(code=429)
        _, rep = run(db, net)
        self.assertIn('429', rep['stopped']); self.assertEqual(rep['states'], {'unknown': 1})
        self.assertEqual(set(h.split('//')[1] for h in net.hosts), {'eth.drpc.org'})
        self.assertEqual(Store(db).ambiguous_rows(1), 2)

    def test_unwatched_and_unsupported_chain(self):
        db = os.path.join(tempfile.mkdtemp(), 'x.db')
        with redirect_stdout(io.StringIO()): self.assertEqual(cli.main(['--db', db, 'verify-traces', ME]), 2)

    def test_max_limits_calls(self):
        db = os.path.join(tempfile.mkdtemp(), 't.db'); s = Store(db); t = s.add_target('wallet', '1', ME, 'w')
        for i in range(3):
            s.add_txs(t, [{'hash': '0x' + str(i) * 64, 'block': 10, 'ts': 1, 'frm': C, 'to': ME, 'value': '5', 'failed': 0, 'kind': 'internal'}] * 2)
        s.db.close()
        _, rep = run(db, Net([tr([0], C, ME, 5)]), '--max', '1')
        self.assertEqual((rep['checked'], rep['not_attempted']), (1, 2))


if __name__ == '__main__':
    unittest.main()
