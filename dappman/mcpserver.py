"""Local MCP server over stdio (newline-delimited JSON-RPC, per the MCP stdio transport) so AI agents such as
Claude Desktop can READ the local dappman data and ask live read-only questions. Loopback-class by design:
stdio only, no network listener, no secrets, no writes. The tool surface is read-only; the Etherscan client's
own allowlist additionally refuses anything that is not a documented read tool.

Claude Desktop config (claude_desktop_config.json):
  {"mcpServers": {"dappman": {"command": "python3", "args": ["-m", "dappman", "--db", "/path/to/dappman.db", "mcp-serve"]}}}
"""
import json, re, sys
from .client import clean

_ADDR = re.compile(r'^0x[0-9a-fA-F]{40}$')
_HASH = re.compile(r'^0x[0-9a-fA-F]{64}$')
_CHAIN = re.compile(r'^[0-9]{1,10}$')
MAX_RESULT_CHARS = 200_000


def _addr(v, required=False):
    if v in (None, ''):
        if required: raise ValueError('address required')
        return ''
    if not isinstance(v, str) or not _ADDR.match(v): raise ValueError('invalid address')
    return v.lower()


def _chain(v):
    v = str(v if v not in (None, '') else '1')
    if not _CHAIN.match(v): raise ValueError('invalid chain id')
    return v


def _limit(v, default=25, cap=200):
    try: n = int(v if v not in (None, '') else default)
    except (TypeError, ValueError): raise ValueError('invalid limit')
    return max(1, min(n, cap))


def _text(obj):
    t = json.dumps(obj, ensure_ascii=False)
    if len(t) > MAX_RESULT_CHARS:
        t = json.dumps({'truncated': True, 'note': 'result too large; narrow with address/limit'})
    return {'content': [{'type': 'text', 'text': t}]}


def make_tools(store_factory, client_factory):
    """store_factory() must yield a read-only Store; client_factory() a fresh read-only McpClient."""
    def _store():
        return store_factory()

    def list_targets(_a):
        return _text({'targets': _store().targets()})

    def get_transactions(a):
        st = _store()
        addr = _addr(a.get('address'))
        limit = _limit(a.get('limit'))
        kinds = a.get('kinds')
        if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
            raise ValueError('kinds must be a list of strings')
        out = []
        for t in st.targets():
            if addr and t['address'] != addr:
                continue
            for r in st.recent_txs(t['id'], limit):
                if kinds and r['kind'] not in kinds:
                    continue
                out.append({**r, 'target_label': t['label'], 'chain': t['chain']})
        amb = sum(st.ambiguous_rows(t['id']) for t in st.targets() if not addr or t['address'] == addr)
        leg = sum(st.legacy_rows(t['id']) for t in st.targets() if not addr or t['address'] == addr)
        return _text({'transactions': out[:limit], 'identity_ambiguous_rows': amb, 'legacy_rows_excluded': leg,
                      'note': 'stored rows only; coverage per target applies; completeness unproven (provider gives no log index)'})

    def get_alerts(a):
        return _text({'alerts': _store().alerts(_limit(a.get('limit'), 30))})

    def get_nft_inventory(a):
        from .nfts import derive
        st = _store()
        addr = _addr(a.get('address'))
        out = []
        for t in st.targets():
            if addr and t['address'] != addr:
                continue
            d = derive(st, t['id'], t['address'])
            d['items'] = d['items'][:200]
            out.append({'address': t['address'], 'chain': t['chain'], 'derived_inventory': d['items'],
                        'unconfirmed': True, 'window': d['window'],
                        'identity_ambiguous_rows': d['identity_ambiguous_rows'], 'legacy_rows_excluded': d['legacy_rows_excluded']})
        return _text({'inventories': out})

    def confirm_nfts(a):
        from .nfts import derive
        from .chainrpc import confirm_items
        st = _store()
        addr = _addr(a.get('address'), True)
        out = []
        for t in st.targets():
            if t['address'] != addr or t['kind'] != 'wallet':
                continue
            if a.get('chain') and t['chain'] != _chain(a.get('chain')):
                continue
            d = derive(st, t['id'], addr)
            out.append(confirm_items(t['chain'], addr, d['items'], max_items=_limit(a.get('limit'), 25, 50)))
        return _text({'confirmations': out, 'note': 'read-only eth_call at one pinned block; confirms stored candidates only, does not discover holdings'})

    def check_address(a):
        from . import sanctions, reputation
        addr = _addr(a.get('address'), True)
        chain = _chain(a.get('chain'))
        st = _store()
        return _text({'address': addr, 'ofac_sdn': sanctions.check(st, addr),
                      'goplus': reputation.check(addr, chain)})

    def get_balance(a):
        c = client_factory()
        ad, ch = _addr(a.get('address'), True), _chain(a.get('chain'))
        b = c.call('get_native_balance', {'address': ad, 'chainid': ch, 'tag': 'latest'})
        wei = int(b[0]) if isinstance(b, list) else int(b)
        return _text({'address': ad, 'chain': ch, 'balance_wei': wei,
                      'balance_native': wei / 10**18})

    def tx_status(a):
        from .txstatus import check
        h = a.get('hash')
        if not isinstance(h, str) or not _HASH.match(h): raise ValueError('invalid tx hash')
        return _text(check(client_factory(), _chain(a.get('chain')), h.lower()))

    def gas(a):
        chain = _chain(a.get('chain'))
        g = client_factory().call('get_gas_oracle', {'chainid': chain})
        return _text({'chain': chain, 'unit': 'gwei', 'safe': g['SafeGasPrice'], 'standard': g['ProposeGasPrice'],
                      'fast': g['FastGasPrice'], 'base_fee': g['suggestBaseFee'], 'source': 'Etherscan gas oracle, indicative'})

    A_ADDR = {'address': {'type': 'string', 'description': '0x address'}}
    A_CHAIN = {'chain': {'type': 'string', 'description': 'EVM chain id, default 1 (Ethereum)'}}
    return {
        'dappman_list_targets': (list_targets, 'List watched wallets/contracts/transactions with their chains and labels',
                                 {'type': 'object', 'properties': {}}),
        'dappman_get_transactions': (get_transactions, 'Recent stored transactions for watched addresses (stored rows only, not live chain)',
                                     {'type': 'object', 'properties': {**A_ADDR, 'limit': {'type': 'integer'},
                                       'kinds': {'type': 'array', 'items': {'type': 'string'}}}}),
        'dappman_get_alerts': (get_alerts, 'Latest monitoring alerts (new/failed/large txs, sensitive calls, sanctions hits, gas)',
                               {'type': 'object', 'properties': {'limit': {'type': 'integer'}}}),
        'dappman_get_nft_inventory': (get_nft_inventory, 'DERIVED NFT inventory per watched address (from stored transfers; NOT confirmed holdings)',
                                      {'type': 'object', 'properties': {**A_ADDR}}),
        'dappman_confirm_nfts': (confirm_nfts, 'Confirm derived NFT candidates on-chain (ownerOf/balanceOf, pinned block, keyless public RPC). Not holdings discovery.',
                                 {'type': 'object', 'properties': {**A_ADDR, **A_CHAIN, 'limit': {'type': 'integer'}}, 'required': ['address']}),
        'dappman_check_address': (check_address, 'Check an address against the cached OFAC SDN crypto list (with list date) and GoPlus reputation signals',
                                  {'type': 'object', 'properties': {**A_ADDR, **A_CHAIN}, 'required': ['address']}),
        'dappman_get_balance': (get_balance, 'Live native balance via keyless Etherscan (read-only)',
                                {'type': 'object', 'properties': {**A_ADDR, **A_CHAIN}, 'required': ['address']}),
        'dappman_tx_status': (tx_status, 'Live status of one transaction hash: success/failed/pending/not_found/unavailable',
                              {'type': 'object', 'properties': {'hash': {'type': 'string'}, **A_CHAIN}, 'required': ['hash']}),
        'dappman_gas': (gas, 'Live gas oracle for a chain (gwei, indicative)',
                        {'type': 'object', 'properties': {**A_CHAIN}}),
    }


def handle(tools, msg):
    """One JSON-RPC message -> response dict or None (notifications)."""
    mid, method = msg.get('id'), msg.get('method')
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {
            'protocolVersion': msg.get('params', {}).get('protocolVersion', '2025-03-26'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'dappman-local', 'version': '0.5'}}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [
            {'name': n, 'description': d, 'inputSchema': {**s, 'additionalProperties': False}} for n, (_f, d, s) in tools.items()]}}
    if method == 'tools/call':
        p = msg.get('params') or {}
        name, args = p.get('name'), p.get('arguments') or {}
        if not isinstance(args, dict):
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'arguments must be an object'}}
        if not isinstance(name, str) or name not in tools:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'unknown tool (read-only set only)'}}
        try:
            return {'jsonrpc': '2.0', 'id': mid, 'result': tools[name][0](args)}
        except Exception as e:
            return {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text',
                    'text': 'error: ' + clean(e, 200)}], 'isError': True}}
    if method and method.startswith('notifications/'):
        return None
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not supported'}}


def serve_stdio(tools, inp=None, out=None):
    inp, out = inp or sys.stdin, out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'bad JSON'}}) + '\n')
            out.flush(); continue
        resp = handle(tools, msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + '\n')
            out.flush()
