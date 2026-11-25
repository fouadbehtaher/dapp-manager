"""Scan one target and raise alerts against the previous snapshot. Read-only."""
import hashlib, re
from .abi import selector_matches
from decimal import Decimal
from .client import ApiError

ADDR = re.compile(r'^0x[0-9a-fA-F]{40}$')
CHAINS = {'1': ('Ethereum', 'ETH'), '56': ('BNB Smart Chain', 'BNB'), '137': ('Polygon', 'POL'),
          '42161': ('Arbitrum One', 'ETH'), '8453': ('Base', 'ETH'), '10': ('Optimism', 'ETH'),
          '43114': ('Avalanche C-Chain', 'AVAX')}
PAGE = 50
MAX_PAGES = 5


def norm_addr(a):
    if not ADDR.match(a or ''):
        raise ValueError('invalid address: ' + str(a))
    return a.lower()


def eth(wei, dec=18):
    try: return Decimal(int(wei)) / (Decimal(10) ** dec)
    except Exception: return Decimal(0)


SENSITIVE = {  # function-name fragments (lowercase) -> (level, meaning). Name match only: needs a verified ABI on Etherscan.
    'transferownership': ('critical', 'ownership transfer'), 'renounceownership': ('critical', 'ownership renounced'),
    'upgradeto': ('critical', 'proxy upgrade'), 'changeadmin': ('critical', 'proxy admin change'),
    'setimplementation': ('critical', 'implementation change'), 'grantrole': ('warn', 'role granted'),
    'revokerole': ('warn', 'role revoked'), 'pause': ('warn', 'pause/unpause'), 'blacklist': ('warn', 'blacklist change'),
    'mint': ('warn', 'mint'), 'emergencywithdraw': ('warn', 'emergency withdraw'), 'setowner': ('critical', 'owner set'),
    'setadmin': ('critical', 'admin set'), 'selfdestruct': ('critical', 'self-destruct'),
}


def sensitive(method):
    m = (method or '').lower()
    if m.startswith('0x'):  # methodId only: ABI not verified, cannot name it
        return None
    for frag, (lvl, why) in SENSITIVE.items():
        if frag in m:
            return lvl, why
    return None


def profile(rows, addr, top=5, verified=False):
    """Activity profile over the stored window. Not full history."""
    calls = [r for r in rows if r['to'] == addr]
    methods = {}
    for r in calls:
        methods[r['method'] or '(transfer/unknown)'] = methods.get(r['method'] or '(transfer/unknown)', 0) + 1
    callers = {}
    for r in calls:
        callers[r['frm']] = callers.get(r['frm'], 0) + 1
    sens = []
    for r in calls:
        s = sensitive(r['method']) if verified else None
        if s: sens.append({'ts': r['ts'], 'hash': r['hash'], 'method': r['method'], 'caller': r['frm'],
                           'level': 'info' if r['failed'] else 'warn', 'why': s[1], 'failed': r['failed']})
    return {'calls': len(calls), 'failed': sum(r['failed'] for r in calls),
            'unique_callers': len(callers),
            'top_methods': sorted(methods.items(), key=lambda x: -x[1])[:top],
            'top_callers': sorted(callers.items(), key=lambda x: -x[1])[:top],
            'sensitive': sens[:20],
            'unnamed_methods': sum(1 for r in calls if (r['method'] or '').startswith('0x')), 'abi_verified': verified}


def fmt(d):
    d = Decimal(d)
    if d == 0: return '0'
    if abs(d) < Decimal('0.000001'): return '<0.000001'
    q = f'{d:,.6f}'.rstrip('0').rstrip('.')
    return q


def _method(r):
    """A function NAME is kept only if keccak(signature)[:4] equals the tx input's real selector; otherwise the raw
    method id is kept, so an untrusted/mismatched label can never trigger a sensitive-call alert."""
    fn, mid, inp = r.get('functionName') or '', r.get('methodId') or '', r.get('input') or ''
    if fn and selector_matches(fn, inp):
        return fn.split('(')[0]
    return mid if mid and mid != '0x' else ''


def _tx(r, kind):
    """One stored row per event. internal rows carry no function name/selector (method stays ''),
    so they can never raise a sensitive_call alert. NFT rows keep contract/token_id in their own columns."""
    nft = kind in ('erc721', 'erc1155')
    return {'hash': r.get('hash') or r.get('transactionHash', ''), 'block': int(r.get('blockNumber') or 0),
            'ts': int(r.get('timeStamp') or 0), 'frm': (r.get('from') or '').lower(), 'to': (r.get('to') or '').lower(),
            'value': str(r.get('tokenValue') if nft and kind == 'erc1155' else ('1' if nft else r.get('value') or '0')),
            'failed': 1 if r.get('isError') == '1' else 0,
            'method': _method(r) if not nft else '', 'kind': kind,
            'token': (r.get('tokenSymbol') or '')[:20] + ('|' + str(r.get('tokenDecimal') or '') if kind == 'erc20' else ''),
            'contract': (r.get('contractAddress') or '').lower() if nft else '',
            'token_id': str(r.get('tokenID') or '') if nft else ''}


def scan(client, store, target, big_native=Decimal(10)):
    if target['kind'] == 'tx':
        from .txstatus import tx_scan
        return tx_scan(client, store, target)
    """Returns dict(snapshot, new_alerts, new_txs). Each category records its own status; errors never become zeros."""
    tid, chain, addr, kind = target['id'], target['chain'], target['address'], target['kind']
    sym = CHAINS.get(chain, ('', ''))[1]
    snap = {'chain': chain, 'address': addr, 'kind': kind, 'status': {}}
    prev_ts, prev = store.last_snapshot(tid)
    first = prev is None
    alerts = []

    def attempt(name, fn):
        try:
            v = fn(); snap['status'][name] = 'ok'; return v
        except ApiError as e:
            snap['status'][name] = 'error: ' + str(e); return None

    bal = attempt('balance', lambda: client.call('get_native_balance', {'address': addr, 'chainid': chain, 'tag': 'latest'}))
    if bal is not None:
        try: snap['native_balance'] = str(eth(bal if not isinstance(bal, list) else bal[0]))
        except Exception: snap['status']['balance'] = 'error: unparsed'
    if kind == 'contract':
        cr = attempt('creation', lambda: client.call('get_contract_creation', {'contractaddresses': [addr], 'chainid': chain}))
        if cr:
            snap['creator'] = cr[0].get('contractCreator'); snap['created_ts'] = int(cr[0].get('timestamp') or 0)
            snap['creation_tx'] = cr[0].get('txHash')
        src = attempt('source', lambda: client.call('get_contract_source', {'address': addr, 'chainid': chain}))
        if src:
            s = src[0]; code = s.get('SourceCode') or ''
            snap['verified'] = bool(code)
            snap['name'] = s.get('ContractName') or ''
            snap['compiler'] = s.get('CompilerVersion') or ''
            snap['source_hash'] = hashlib.sha256(code.encode()).hexdigest() if code else ''
            snap['proxy'] = s.get('Proxy') == '1'
            snap['implementation'] = (s.get('Implementation') or '').lower()
    known = store.known_hashes(tid)
    gaps = []

    def pages(name, tool, args, kind_):
        """Page newest-first until a known record is reached (so a busy contract is not silently truncated).
        Baseline reads one page. If MAX_PAGES pass without reaching a known record, flag a possible gap."""
        def run():
            out = []
            for pg in range(1, MAX_PAGES + 1):
                rr = client.call(tool, {**args, 'chainid': chain, 'page': pg, 'offset': PAGE, 'sort': 'desc'})
                out += [_tx(r, kind_) for r in rr]
                if first or len(rr) < PAGE or any((r['hash'], kind_) in known for r in out[-len(rr):]):
                    return out
            if prev.get('status', {}).get(name) == 'ok':
                gaps.append(name)
            return out
        return attempt(name, run) or []

    rows = pages('transactions', 'get_transactions', {'address': addr}, 'normal')
    rows += pages('internal_transactions', 'get_internal_transactions', {'address': addr}, 'internal')
    for std, cat in (('erc20', 'token_transfers'), ('erc721', 'nft_721'), ('erc1155', 'nft_1155')):
        targs = {'contractaddress': addr, 'standard': std} if kind == 'contract' else {'address': addr, 'standard': std}
        rows += pages(cat, 'get_token_transfers', targs, std)
    snap['coverage'] = (f'baseline = latest {PAGE} per category; later scans page back to the last known record '
                        f'(max {MAX_PAGES * PAGE}). Categories: normal/internal/ERC-20/ERC-721/ERC-1155. '
                        f'Not full history unless `dappman sync` ran (bounded sync, see its own limits).')
    snap['gaps'] = gaps
    fresh = [r for r in rows if (r['hash'], r['kind']) not in known]
    store.add_txs(tid, rows)
    snap['tx_rows_stored'] = store.tx_count(tid)

    if not first:  # alerts only after a baseline exists
        newn = [r for r in fresh if r['kind'] == 'normal']
        if newn:
            alerts.append(('info', 'new_tx', f'{len(newn)} new transactions (latest block {max(r["block"] for r in newn)})'))
        fails = [r for r in newn if r['failed']]
        if fails:
            alerts.append(('warn', 'failed_tx', f'{len(fails)} new failed transactions'))
        if kind == 'contract' and snap.get('verified') is True:
            for r in newn:
                sv = sensitive(r['method']) if r['to'] == addr else None  # direct external tx TO this contract only
                if sv:
                    if r['failed']:
                        alerts.append(('info', 'sensitive_call', f"review: reverted call to {r['method']}() ({sv[1]}); reverted, no state change expected; by {r['frm'][:10]}... in {r['hash'][:12]}..."))
                    else:
                        alerts.append(('warn', 'sensitive_call', f"review priority: successful tx called {r['method']}() ({sv[1]}) by {r['frm'][:10]}... in {r['hash'][:12]}... Name/selector match only; not proof of an attack or of the resulting state, check the tx"))
        big = [r for r in newn if eth(r['value']) >= big_native]
        for r in big:
            alerts.append(('warn', 'large_value', f'native value {fmt(eth(r["value"]))} {sym} (native coin units, not USD) in {r["hash"][:12]}...'))
        newt = [r for r in fresh if r['kind'] == 'erc20']
        if newt:
            alerts.append(('info', 'new_token_transfers', f'{len(newt)} new ERC-20 transfers'))
        newi = [r for r in fresh if r['kind'] == 'internal']
        if newi:
            alerts.append(('info', 'new_internal_txs', f'{len(newi)} new internal transactions (contract-executed value movement; no function names visible)'))
        newn2 = [r for r in fresh if r['kind'] in ('erc721', 'erc1155')]
        if newn2:
            alerts.append(('info', 'new_nft_transfers', f'{len(newn2)} new NFT transfers (ERC-721/1155 events; a transfer event is not proof of current ownership)'))
        bigi = [r for r in newi if eth(r['value']) >= big_native]
        for r in bigi:
            alerts.append(('warn', 'large_value_internal', f'internal movement of native value {fmt(eth(r["value"]))} {sym} (native coin units, not USD) in {r["hash"][:12]}...'))
    from . import sanctions
    ofac = sanctions.load(store)
    if ofac is None:
        snap['sanctions_note'] = 'off: OFAC list not downloaded (dappman sanctions-update)'
    else:
        snap['sanctions_list_date'] = ofac['publish_date']
    if not first:  # more alerts only after a baseline exists
        if ofac is not None:
            seen_cp = set()
            for r in fresh:
                for cp in (r['frm'], r['to']):
                    if not cp or cp == addr or cp in seen_cp:
                        continue
                    seen_cp.add(cp)
                    hit = ofac['entries'].get(cp)
                    if hit:
                        alerts.append(('critical', 'sanctioned_counterparty',
                            f"counterparty {cp[:10]}... appears on the OFAC SDN list as '{hit['entity']}' ({'/'.join(hit['currencies'])} entry, list dated {ofac['publish_date']}) in {r['hash'][:12]}... Presence on the list is a fact about the list, not a legal conclusion; verify before acting"))
        for k, code, lvl in (('verified', 'verification_changed', 'warn'), ('source_hash', 'source_changed', 'critical'),
                             ('implementation', 'implementation_changed', 'critical')):
            if k in prev and k in snap and prev[k] != snap[k]:
                alerts.append((lvl, code, f'{k}: {prev[k]!r} -> {snap[k]!r}'))
        for name, st in snap['status'].items():
            if st != 'ok' and prev.get('status', {}).get(name) == 'ok':
                alerts.append(('warn', 'category_failed', f'{name} failed now: {st}'))
    if gaps:
        alerts.append(('warn', 'coverage_gap', f'possible missed records in {", ".join(gaps)}: more than {MAX_PAGES*PAGE} new since last scan. Scan more often; alerts here are incomplete.'))
    for lvl, code, msg in alerts:
        store.add_alert(tid, lvl, code, msg)
    snap['baseline'] = first
    store.save_snapshot(tid, snap)
    return {'snapshot': snap, 'new_alerts': alerts, 'new_txs': len(fresh)}


def scan_all(client, store, big=Decimal(10)):
    """One full pass: every target, then one gas sample per chain in use. Returns list of (target, result)."""
    from .txstatus import gas_scan
    res = [(t, scan(client, store, t, big)) for t in store.targets()]
    gas_scan(client, store, [t['chain'] for t in store.targets()])
    return res
