"""Receipt-derived event identity: resolves the 'identical twins' ambiguity for NFT transfers the provider already returned.

The free provider gives no log index, so byte-identical events inside one transaction cannot be told apart from provider
duplicates. A transaction receipt (eth_getTransactionReceipt) lists every log with its logIndex. For the NFT transfers
(ERC-721 Transfer, ERC-1155 TransferSingle / TransferBatch) of a tx we already hold, the receipt is the raw on-chain
record, so we derive the events from it and RECONCILE with the provider rows instead of forcing the counts to match.

What this does NOT do: find transactions the provider never returned (that needs a log index source we do not have
keyless), cover ERC-20/internal twins, or prove finality. States per tx, never silently collapsed:
  verified | pending (too few confirmations) | unknown (no usable answer, reason kept) | inconsistent (receipt contradicts the
  stored row) | reorg_suspected (block hash differs from the last check).
Only 'verified' receipts replace provider rows when deriving inventory.
Endpoint policy: endpoints are tried in the configured order only when one gives NO answer (null receipt / network error).
HTTP 401/403/429 means the endpoint refuses us: the run stops, nothing is retried elsewhere, no endpoint is rotated to get around it.
"""
import json, time, urllib.request, urllib.error
from . import config
from .client import clean

T_TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
T_SINGLE = '0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'
T_BATCH = '0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb'
FINALITY_DEPTH = 12   # confirmations required before a receipt is used; a convention, not a finality proof
NFT_KINDS = ('erc721', 'erc1155')


def _addr(topic):
    return '0x' + topic[-40:].lower()


def _word(data, i):
    return int(data[2 + 64 * i: 2 + 64 * (i + 1)] or '0', 16)


def parse_logs(logs):
    """Receipt logs -> NFT events with logIndex. Raises ValueError on a malformed log (the tx then stays 'unknown')."""
    out = []
    for lg in logs or []:
        if not isinstance(lg, dict) or lg.get('removed'):
            continue
        tp, data = lg.get('topics') or [], lg.get('data') or '0x'
        if not tp or not isinstance(tp[0], str):
            continue
        t0, c = tp[0].lower(), (lg.get('address') or '').lower()
        try:
            li = int(lg['logIndex'], 16)
            if t0 == T_TRANSFER and len(tp) == 4:           # ERC-721 (ERC-20 has 3 topics and is not handled here)
                out.append({'log_index': li, 'sub': 0, 'kind': 'erc721', 'contract': c, 'token_id': str(int(tp[3], 16)),
                            'frm': _addr(tp[1]), 'to': _addr(tp[2]), 'value': '1'})
            elif t0 == T_SINGLE and len(tp) == 4:
                if len(data) < 2 + 128: raise ValueError('short TransferSingle data')
                out.append({'log_index': li, 'sub': 0, 'kind': 'erc1155', 'contract': c, 'token_id': str(_word(data, 0)),
                            'frm': _addr(tp[2]), 'to': _addr(tp[3]), 'value': str(_word(data, 1))})
            elif t0 == T_BATCH and len(tp) == 4:
                oi, ov = _word(data, 0) // 32, _word(data, 1) // 32
                n, nv = _word(data, oi), _word(data, ov)
                if n != nv or n > 10000 or len(data) < 2 + 64 * (max(oi, ov) + 1 + n):
                    raise ValueError('inconsistent TransferBatch arrays')
                for k in range(n):
                    out.append({'log_index': li, 'sub': k, 'kind': 'erc1155', 'contract': c, 'token_id': str(_word(data, oi + 1 + k)),
                                'frm': _addr(tp[2]), 'to': _addr(tp[3]), 'value': str(_word(data, ov + 1 + k))})
        except (ValueError, KeyError, TypeError, IndexError) as e:
            raise ValueError('malformed log: ' + clean(e, 60))
    return out


class ReceiptSource:
    def __init__(self, chain, urls=None, opener=None, timeout=15, delay=0.25):
        self.chain = str(chain)
        self.urls = list(urls if urls is not None else config.RECEIPT_RPC_URLS.get(self.chain, config.RPC_URLS.get(self.chain, [])))
        self.opener, self.timeout, self.delay = opener or urllib.request.urlopen, timeout, delay
        self.stopped = None          # set when an endpoint refused us: the run ends, no rotation
        self._chain_ok = {}
        self.calls = 0

    def _post(self, url, method, params):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
        req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'User-Agent': 'dappman (read-only)'})
        self.calls += 1
        if self.delay and self.calls > 1:
            time.sleep(self.delay)
        return json.loads(self.opener(req, timeout=self.timeout).read().decode())

    def _check_chain(self, url):
        if url not in self._chain_ok:
            d = self._post(url, 'eth_chainId', [])
            self._chain_ok[url] = isinstance(d.get('result'), str) and str(int(d['result'], 16)) == self.chain
        return self._chain_ok[url]

    def tip(self):
        """Latest block from the first endpoint that answers; None if none does (every tx then stays unknown)."""
        for u in self.urls:
            if self.stopped:
                return None
            try:
                if not self._check_chain(u): continue
                r = self._post(u, 'eth_blockNumber', []).get('result')
                if isinstance(r, str): return int(r, 16)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429): self.stopped = 'HTTP %s from %s' % (e.code, u.split('//')[-1]); return None
            except Exception:
                continue
        return None

    def get(self, tx_hash):
        """-> {'state':'ok','receipt':dict,'host':h} | {'state':'unknown','reason':str,'host':h|None}"""
        if self.stopped:
            return {'state': 'unknown', 'reason': 'run stopped: endpoint refused (%s); not retried elsewhere' % self.stopped, 'host': None}
        last = 'no receipt endpoint configured for chain ' + self.chain
        for u in self.urls:
            host = u.split('//')[-1]
            try:
                if not self._check_chain(u):
                    last = '%s: wrong chain id' % host; continue
                d = self._post(u, 'eth_getTransactionReceipt', [tx_hash])
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    self.stopped = 'HTTP %s from %s' % (e.code, host)
                    return {'state': 'unknown', 'reason': 'endpoint refused: ' + self.stopped + '; not retried elsewhere', 'host': host}
                last = '%s: HTTP %s' % (host, e.code); continue
            except Exception as e:
                last = '%s: %s' % (host, clean(e, 70)); continue
            if not isinstance(d, dict):
                last = '%s: unrecognised reply' % host; continue
            if d.get('error'):
                last = '%s: rpc error %s' % (host, clean(d['error'], 70)); continue
            r = d.get('result')
            if r is None:
                last = '%s: receipt not available (null)' % host; continue
            if not isinstance(r, dict):
                last = '%s: unrecognised receipt' % host; continue
            return {'state': 'ok', 'receipt': r, 'host': host}
        return {'state': 'unknown', 'reason': clean(last, 140), 'host': None}


def assess(receipt, tx_hash, stored_block, tip):
    """Receipt vs what we stored -> (state, reason, events). Contradictions are reported, never repaired silently."""
    if not isinstance(receipt, dict) or not receipt.get('transactionHash') or 'blockNumber' not in receipt:
        return 'unknown', 'malformed receipt (missing transactionHash/blockNumber)', []
    try:
        if (receipt.get('transactionHash') or '').lower() != tx_hash.lower():
            return 'inconsistent', 'receipt is for a different transaction hash', []
        bn, bh = int(receipt['blockNumber'], 16), (receipt.get('blockHash') or '').lower()
        if bn != int(stored_block):
            return 'inconsistent', 'receipt block %d differs from stored block %s' % (bn, stored_block), []
        for lg in receipt.get('logs') or []:
            if (lg.get('blockHash') or '').lower() != bh or (lg.get('transactionHash') or '').lower() != tx_hash.lower():
                return 'inconsistent', 'a log does not belong to the receipt block/tx', []
        events = parse_logs(receipt.get('logs'))
    except ValueError as e:
        return 'unknown', clean(e, 100), []
    except (KeyError, TypeError, AttributeError) as e:
        return 'unknown', 'malformed receipt: ' + clean(e, 60), []
    if tip is None:
        return 'unknown', 'chain tip unavailable, cannot judge confirmations', events
    if tip - bn < FINALITY_DEPTH:
        return 'pending', 'only %d confirmations (< %d)' % (tip - bn, FINALITY_DEPTH), events
    return 'verified', 'tx failed on-chain (no events)' if receipt.get('status') == '0x0' else '', events


def verify(store, tid, wallet, chain, src, hashes, progress=None):
    """Verify the given tx hashes. Returns a report; every tx ends in an explicit state."""
    wallet = wallet.lower()
    tip = src.tip()
    rep = {'chain': str(chain), 'tip': tip, 'checked': 0, 'states': {}, 'provider_mismatch': [], 'stopped': None, 'host': None}
    for h in hashes:
        if src.stopped: break
        blk = store.db.execute("SELECT MAX(block) FROM txs WHERE target_id=? AND hash=? AND kind IN ('erc721','erc1155')", (tid, h)).fetchone()[0]
        got = src.get(h)
        if got['state'] != 'ok':
            state, reason, events, host, bh = 'unknown', got['reason'], [], got['host'], ''
        else:
            state, reason, events = assess(got['receipt'], h, blk if blk is not None else -1, tip)
            host, bh = got['host'], (got['receipt'].get('blockHash') or '').lower()
        mine = [e for e in events if e['frm'] == wallet or e['to'] == wallet]
        mm = store.save_receipt(tid, h, chain, blk, bh, state, reason, host, tip, mine)
        rep['states'][state] = rep['states'].get(state, 0) + 1
        rep['checked'] += 1; rep['host'] = host or rep['host']
        if mm: rep['provider_mismatch'].append({'hash': h, **mm})
        if progress: progress(h, state)
    rep['stopped'] = src.stopped
    return rep
