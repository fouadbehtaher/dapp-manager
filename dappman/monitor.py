"""Scan one target and raise alerts against the previous snapshot. Read-only."""
import hashlib, re
from decimal import Decimal
from .client import ApiError

ADDR = re.compile(r'^0x[0-9a-fA-F]{40}$')
CHAINS = {'1': ('Ethereum', 'ETH'), '137': ('Polygon', 'POL'), '42161': ('Arbitrum One', 'ETH')}
PAGE = 50
MAX_PAGES = 5


def norm_addr(a):
    if not ADDR.match(a or ''):
        raise ValueError('invalid address: ' + str(a))
    return a.lower()


def eth(wei, dec=18):
    try: return Decimal(int(wei)) / (Decimal(10) ** dec)
    except Exception: return Decimal(0)


def fmt(d):
    d = Decimal(d)
    if d == 0: return '0'
    if abs(d) < Decimal('0.000001'): return '<0.000001'
    q = f'{d:,.6f}'.rstrip('0').rstrip('.')
    return q


def _tx(r, kind):
    return {'hash': r.get('hash') or r.get('transactionHash', ''), 'block': int(r.get('blockNumber') or 0),
            'ts': int(r.get('timeStamp') or 0), 'frm': (r.get('from') or '').lower(), 'to': (r.get('to') or '').lower(),
            'value': str(r.get('value') or '0'), 'failed': 1 if r.get('isError') == '1' else 0,
            'method': (r.get('functionName') or r.get('methodId') or '').split('(')[0], 'kind': kind,
            'token': (r.get('tokenSymbol') or '')[:20] + ('|' + str(r.get('tokenDecimal') or '') if kind == 'erc20' else '')}


def scan(client, store, target, big_native=Decimal(10)):
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
    targs = {'contractaddress': addr, 'standard': 'erc20'} if kind == 'contract' else {'address': addr, 'standard': 'erc20'}
    rows += pages('token_transfers', 'get_token_transfers', targs, 'erc20')
    snap['coverage'] = (f'baseline = latest {PAGE} per category; later scans page back to the last known record '
                        f'(max {MAX_PAGES * PAGE}). Not full history.')
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
        big = [r for r in newn if eth(r['value']) >= big_native]
        for r in big:
            alerts.append(('warn', 'large_value', f'native value {fmt(eth(r["value"]))} {sym} (native coin units, not USD) in {r["hash"][:12]}...'))
        newt = [r for r in fresh if r['kind'] == 'erc20']
        if newt:
            alerts.append(('info', 'new_token_transfers', f'{len(newt)} new ERC-20 transfers'))
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
