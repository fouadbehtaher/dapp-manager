"""Trace-based check of internal ETH transfers (read-only, optional; used by `verify-traces`, not by sync).

Receipts do not contain internal value transfers, so byte-identical internal rows from the provider stay ambiguous. A
Parity-style trace_transaction lists every call with its traceAddress, so identical twins become distinct calls.
TraceSource fetches one trace per tx from the configured keyless endpoint (HTTP 401/403/429 stops the run, no rotation).
Only state 'match' clears provider rows from the ambiguous count. Nothing is claimed about transactions the provider never returned.
Reconcile states: match | trace_has_more | provider_has_more | unknown (trace missing or unusable, reason kept).
A failed call (error set) moves no value and is excluded. Top-level calls (empty traceAddress) are the normal tx, not internal.
"""
from collections import Counter


def _hex(v):
    return int(v, 16) if isinstance(v, str) and v.startswith('0x') else int(v or 0)


def parse_trace(entries):
    """trace_transaction result -> list of internal value moves {'trace': tuple, 'frm','to','value'} (wei, str). ValueError if malformed."""
    if not isinstance(entries, list):
        raise ValueError('trace is not a list')
    out = []
    for e in entries:
        try:
            ta = tuple(e.get('traceAddress') or [])
            if e.get('error') or not ta:
                continue
            a, t = e.get('action') or {}, e.get('type')
            if t == 'call':
                if a.get('callType') in ('delegatecall', 'staticcall'):
                    continue            # these never move value
                v = _hex(a.get('value'))
                frm, to = a['from'], a['to']
            elif t == 'suicide':
                v, frm, to = _hex(a.get('balance')), a['address'], a['refundAddress']
            else:
                continue                # create / reward: out of scope, not claimed
            if v > 0:
                out.append({'trace': ta, 'frm': frm.lower(), 'to': to.lower(), 'value': str(v)})
        except (KeyError, TypeError, ValueError, AttributeError) as ex:
            raise ValueError('malformed trace entry: %s' % type(ex).__name__)
    return out


def reconcile(trace_moves, provider_rows, address):
    """Compare moves touching `address` with provider internal rows (dicts with frm,to,value) of the same tx."""
    me = address.lower()
    key = lambda f, t, v: (f.lower(), t.lower(), str(v))
    tr = Counter(key(m['frm'], m['to'], m['value']) for m in trace_moves if me in (m['frm'], m['to']))
    pr = Counter(key(r['frm'], r['to'], r['value']) for r in provider_rows if me in (r['frm'].lower(), r['to'].lower()))
    more_t, more_p = tr - pr, pr - tr
    state = 'match' if not more_t and not more_p else 'trace_has_more' if more_t and not more_p else 'provider_has_more' if more_p and not more_t else 'mismatch'
    return {'state': state, 'trace_count': sum(tr.values()), 'provider_count': sum(pr.values()),
            'only_trace': sum(more_t.values()), 'only_provider': sum(more_p.values())}


from . import config
from .receipts import ReceiptSource, FINALITY_DEPTH
from .client import clean
import urllib.error


class TraceSource(ReceiptSource):
    def __init__(self, chain, urls=None, opener=None, timeout=25, delay=0.5):
        super().__init__(chain, urls=urls if urls is not None else config.TRACE_RPC_URLS.get(str(chain), []), opener=opener, timeout=timeout, delay=delay)

    def get_trace(self, h):
        if self.stopped:
            return {'state': 'unknown', 'reason': 'run stopped: endpoint refused (%s); not retried elsewhere' % self.stopped, 'host': None}
        last = 'no trace endpoint configured for chain ' + self.chain
        for u in self.urls:
            host = u.split('//')[-1]
            try:
                if not self._check_chain(u):
                    last = '%s: wrong chain id' % host; continue
                d = self._post(u, 'trace_transaction', [h])
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    self.stopped = 'HTTP %s from %s' % (e.code, host)
                    return {'state': 'unknown', 'reason': 'endpoint refused: ' + self.stopped, 'host': host}
                last = '%s: HTTP %s' % (host, e.code); continue
            except Exception as e:
                last = '%s: %s' % (host, clean(e, 70)); continue
            if not isinstance(d, dict) or d.get('error') or not isinstance(d.get('result'), list) or not d['result']:
                last = '%s: no usable trace (%s)' % (host, clean((d or {}).get('error') if isinstance(d, dict) else d, 60)); continue
            return {'state': 'ok', 'trace': d['result'], 'host': host}
        return {'state': 'unknown', 'reason': clean(last, 140), 'host': None}


def verify_traces(store, tid, wallet, chain, src, hashes):
    wallet = wallet.lower()
    tip = src.tip()
    rep = {'chain': str(chain), 'tip': tip, 'checked': 0, 'states': {}, 'stopped': None, 'host': None}
    for h in hashes:
        if src.stopped: break
        blk = store.db.execute("SELECT MAX(block) FROM txs WHERE target_id=? AND hash=? AND kind='internal'", (tid, h)).fetchone()[0]
        got = src.get_trace(h)
        rec, state, reason = {}, 'unknown', got.get('reason', '')
        if got['state'] == 'ok':
            try:
                tr = got['trace']
                if any((e.get('transactionHash') or h).lower() != h.lower() for e in tr):
                    state, reason = 'inconsistent', 'trace entry belongs to a different transaction'
                elif blk is None or any(e.get('blockNumber') is not None and int(e['blockNumber']) != int(blk) for e in tr):
                    state, reason = 'inconsistent', 'trace block differs from stored block %s' % blk
                elif tip is None:
                    state, reason = 'unknown', 'chain tip unavailable, cannot judge confirmations'
                elif tip - int(blk) < FINALITY_DEPTH:
                    state, reason = 'pending', 'only %d confirmations (< %d)' % (tip - int(blk), FINALITY_DEPTH)
                else:
                    prov = [{'frm': r[0], 'to': r[1], 'value': r[2]} for r in
                            store.db.execute("SELECT frm,to_,value FROM txs WHERE target_id=? AND hash=? AND kind='internal' AND failed=0", (tid, h))]
                    rec = reconcile(parse_trace(tr), prov, wallet)
                    state, reason = rec['state'], '' if rec['state'] == 'match' else 'trace and provider rows differ: only_trace=%d only_provider=%d' % (rec['only_trace'], rec['only_provider'])
            except (ValueError, TypeError, AttributeError) as e:
                state, reason = 'unknown', 'unusable trace: ' + clean(e, 80)
        store.save_trace(tid, h, chain, blk, state, reason, got.get('host'), rec)
        rep['states'][state] = rep['states'].get(state, 0) + 1
        rep['checked'] += 1; rep['host'] = got.get('host') or rep['host']
    rep['stopped'] = src.stopped
    return rep
