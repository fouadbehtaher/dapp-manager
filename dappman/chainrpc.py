"""Read-only on-chain ownership confirmation through keyless public JSON-RPC (eth_call at a pinned block).

Scope, stated plainly: this CONFIRMS a token the local history already points at (ownerOf / balanceOf).
It does not discover holdings. Every answer is pinned to one block and names the RPC host that gave it.
States: confirmed_owner | not_owner | unknown. 'unknown' always carries a reason; a provider failure, an unsupported
contract or an ambiguous revert is never reported as 'not owned'.
"""
import json, urllib.request
from . import config
from .client import clean

ERC721_IFACE, ERC1155_IFACE = '80ac58cd', 'd9b67a26'


def _pad(h):
    return h[2:].rjust(64, '0') if h.startswith('0x') else h.rjust(64, '0')


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, chain, urls=None, timeout=15, opener=None):
        self.chain, self.urls = str(chain), list(urls or config.RPC_URLS.get(str(chain), []))
        self.timeout, self.opener = timeout, opener or urllib.request.urlopen
        self.host = None

    def _post(self, url, method, params):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
        req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'User-Agent': 'dappman (read-only)'})
        return json.loads(self.opener(req, timeout=self.timeout).read().decode())

    def request(self, method, params):
        """Returns ('ok', result) | ('revert', data) ; raises RpcError when no endpoint gave a usable answer."""
        if not self.urls:
            raise RpcError('no public RPC configured for chain ' + self.chain)
        last = None
        for u in self.urls:
            try:
                d = self._post(u, method, params)
            except Exception as e:
                last = '%s: %s' % (u.split('//')[-1], clean(e, 80)); continue
            self.host = u.split('//')[-1]
            if 'error' in d:
                msg = str(d['error'].get('message', '')).lower()
                if 'revert' in msg or d['error'].get('code') == 3:
                    return 'revert', d['error'].get('data')
                last = '%s: rpc error %s' % (self.host, clean(d['error'], 80)); continue
            return 'ok', d.get('result')
        raise RpcError(last or 'rpc failed')


def pin_block(rpc):
    st, r = rpc.request('eth_blockNumber', [])
    if st != 'ok' or not isinstance(r, str):
        raise RpcError('block number unavailable')
    cid = rpc.request('eth_chainId', [])[1]
    if isinstance(cid, str) and str(int(cid, 16)) != rpc.chain:
        raise RpcError('endpoint %s reports chain %s, expected %s' % (rpc.host, int(cid, 16), rpc.chain))
    return int(r, 16)


def _call(rpc, to, data, block):
    return rpc.request('eth_call', [{'to': to, 'data': data}, hex(block)])


def _supports(rpc, contract, iface, block):
    st, r = _call(rpc, contract, '0x01ffc9a7' + iface.ljust(64, '0'), block)
    return st == 'ok' and isinstance(r, str) and len(r) >= 66 and int(r, 16) == 1


def confirm(rpc, block, standard, contract, token_id, wallet):
    """One candidate -> dict(state, reason, block, rpc). Never raises."""
    out = {'contract': contract, 'token_id': token_id, 'standard': standard, 'block': block}
    try:
        tid = hex(int(token_id))
        if standard == 'erc721':
            if not _supports(rpc, contract, ERC721_IFACE, block):
                return {**out, 'state': 'unknown', 'reason': 'contract does not report ERC-721 via ERC-165 (non-standard or not a contract)', 'rpc': rpc.host}
            st, r = _call(rpc, contract, '0x6352211e' + _pad(tid), block)
            if st == 'revert':
                return {**out, 'state': 'unknown', 'reason': 'ownerOf reverted: token does not exist/burned or non-standard behaviour; not confirmed owned', 'rpc': rpc.host}
            owner = '0x' + r[-40:].lower()
            return {**out, 'state': 'confirmed_owner' if owner == wallet.lower() else 'not_owner', 'owner': owner, 'rpc': rpc.host}
        if standard == 'erc1155':
            if not _supports(rpc, contract, ERC1155_IFACE, block):
                return {**out, 'state': 'unknown', 'reason': 'contract does not report ERC-1155 via ERC-165', 'rpc': rpc.host}
            st, r = _call(rpc, contract, '0x00fdd58e' + _pad(wallet) + _pad(tid), block)
            if st == 'revert':
                return {**out, 'state': 'unknown', 'reason': 'balanceOf reverted', 'rpc': rpc.host}
            bal = int(r, 16)
            return {**out, 'state': 'confirmed_owner' if bal > 0 else 'not_owner', 'balance': str(bal), 'rpc': rpc.host}
        return {**out, 'state': 'unknown', 'reason': 'unsupported standard', 'rpc': rpc.host}
    except Exception as e:
        return {**out, 'state': 'unknown', 'reason': 'rpc failure: ' + clean(e, 100), 'rpc': rpc.host}


def confirm_items(chain, wallet, items, opener=None, max_items=50):
    """Confirm derived candidates (from nfts.derive). Returns dict(chain, block, rpc, results, skipped, note)."""
    rpc = Rpc(chain, opener=opener)
    try:
        block = pin_block(rpc)
    except RpcError as e:
        return {'chain': str(chain), 'block': None, 'rpc': rpc.host, 'results': [], 'skipped': len(items),
                'error': 'confirmation unavailable: ' + clean(e, 120),
                'note': 'every candidate stays UNCONFIRMED'}
    res = []
    for it in items[:max_items]:
        std = 'erc1155' if it.get('kind') == 'erc1155' else 'erc721'
        res.append(confirm(rpc, block, std, it['contract'], it['token_id'], wallet))
    return {'chain': str(chain), 'block': block, 'rpc': rpc.host, 'results': res, 'skipped': max(0, len(items) - max_items),
            'note': 'confirms listed candidates only, at one pinned block; does NOT discover holdings the local history lacks'}
