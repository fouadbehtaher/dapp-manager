import unittest
from dappman.traces import parse_trace, reconcile
A, B, C = '0x' + 'a' * 40, '0x' + 'b' * 40, '0x' + 'c' * 40


def call(ta, f, t, v, ct='call', err=None):
    e = {'type': 'call', 'traceAddress': ta, 'action': {'from': f, 'to': t, 'value': hex(v), 'callType': ct}}
    if err: e['error'] = err
    return e


class T(unittest.TestCase):
    def test_twins_become_two_calls(self):
        m = parse_trace([call([], B, C, 0), call([0], C, A, 5), call([1], C, A, 5)])
        self.assertEqual(len(m), 2)
        self.assertEqual(reconcile(m, [{'frm': C, 'to': A, 'value': '5'}] * 2, A)['state'], 'match')

    def test_provider_collapsed_twin(self):
        m = parse_trace([call([0], C, A, 5), call([1], C, A, 5)])
        r = reconcile(m, [{'frm': C, 'to': A, 'value': '5'}], A)
        self.assertEqual((r['state'], r['only_trace']), ('trace_has_more', 1))

    def test_excluded(self):
        m = parse_trace([call([0], C, A, 5, err='Reverted'), call([1], C, A, 5, ct='delegatecall'), call([], B, C, 9), call([2], C, A, 0)])
        self.assertEqual(m, [])

    def test_suicide_and_other_addresses(self):
        m = parse_trace([{'type': 'suicide', 'traceAddress': [0], 'action': {'address': C, 'refundAddress': A, 'balance': '0x7'}}, call([1], B, C, 3)])
        self.assertEqual(len(m), 2)
        self.assertEqual(reconcile(m, [{'frm': C, 'to': A, 'value': '7'}], A)['state'], 'match')

    def test_malformed(self):
        with self.assertRaises(ValueError): parse_trace({'x': 1})
        with self.assertRaises(ValueError): parse_trace([{'type': 'call', 'traceAddress': [0], 'action': {}}])
