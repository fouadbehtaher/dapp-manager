"""Transaction receipt status, tx tracking and gas sampling. Read-only (Etherscan keyless MCP).

Status classes (never collapsed):
  success      receipt status "1"
  failed       receipt status "0" (reverted/out of gas; errDescription from get_transaction_status)
  pending      Etherscan knows the tx but it has no block yet
  not_found    Etherscan returns nothing for this hash on this chain (never broadcast, dropped, wrong chain,
               or not indexed yet: these cannot be told apart from here)
  unavailable  an API/network error or an inconsistent answer: the status is UNKNOWN, not 'ok'
NOTE: get_transaction_status alone says isError=0 for a hash that does not exist, so it is never used by itself."""
import re, time
from decimal import Decimal
from .client import ApiError, clean

HASH = re.compile(r'^0x[0-9a-fA-F]{64}$')


def norm_hash(h):
    if not HASH.match(h or ''):
        raise ValueError('invalid tx hash (need 0x + 64 hex): ' + str(h))
    return h.lower()


def _int(x):
    try: return int(x, 16) if isinstance(x, str) and x.startswith('0x') else int(x)
    except Exception: return None


def gwei(wei):
    v = _int(wei)
    return None if v is None else str(Decimal(v) / Decimal(10) ** 9)


def check(client, chain, txhash):
    """Return dict(status, detail...). Never raises; API trouble becomes status='unavailable'."""
    out = {'chain': chain, 'hash': txhash, 'status': 'unavailable', 'detail': ''}
    try:
        tx = client.call('get_transaction_by_hash', {'txhash': txhash, 'chainid': chain})
    except Exception as e:
        out['detail'] = 'lookup error: ' + clean(e, 120); return out
    if not tx:
        out['status'] = 'not_found'
        out['detail'] = 'Etherscan has no record of this hash on this chain (not broadcast / dropped / wrong chain / not indexed yet)'
        return out
    out.update({'from': (tx.get('from') or '').lower(), 'to': (tx.get('to') or '').lower(), 'nonce': _int(tx.get('nonce')),
                'gas_price_gwei': gwei(tx.get('gasPrice')), 'max_fee_gwei': gwei(tx.get('maxFeePerGas'))})
    if not tx.get('blockNumber'):
        out['status'] = 'pending'; out['detail'] = 'known to Etherscan, not yet in a block'
        return out
    out['block'] = _int(tx.get('blockNumber'))
    try:
        rs = client.call('get_transaction_receipt_status', {'txhash': txhash, 'chainid': chain})
        st = (rs or {}).get('status') if isinstance(rs, dict) else None
    except Exception as e:
        out['detail'] = 'receipt error: ' + clean(e, 120); return out
    if st == '1':
        out['status'] = 'success'; out['detail'] = 'receipt status 1 (executed)'
    elif st == '0':
        out['status'] = 'failed'
        try:
            d = client.call('get_transaction_status', {'txhash': txhash, 'chainid': chain})
            out['detail'] = 'receipt status 0 (reverted): ' + ((d or {}).get('errDescription') or 'no reason given by Etherscan')
        except ApiError:
            out['detail'] = 'receipt status 0 (reverted); reason unavailable'
    else:
        out['detail'] = 'in a block but receipt status empty/unknown: treated as unavailable, not as success'
    return out


def tx_scan(client, store, target, stuck_min=30):
    """Scan hook for kind='tx' targets: alert on status transitions and on long-pending."""
    tid, chain, h = target['id'], target['chain'], target['address']
    prev_ts, prev = store.last_snapshot(tid)
    res = check(client, chain, h)
    now = int(time.time())
    res['first_seen'] = (prev or {}).get('first_seen') or now
    res['pending_alerted'] = (prev or {}).get('pending_alerted', False)
    alerts = []
    old = (prev or {}).get('status_cat')
    if old is not None and res['status'] != old:
        lvl = {'success': 'info', 'failed': 'warn', 'pending': 'info', 'not_found': 'info', 'unavailable': 'warn'}[res['status']]
        alerts.append((lvl, 'tx_status_changed', f"{h[:12]}...: {old} -> {res['status']}. {res['detail']}"))
    if res['status'] == 'pending' and not res['pending_alerted'] and now - res['first_seen'] >= stuck_min * 60:
        alerts.append(('warn', 'tx_pending_long', f"{h[:12]}...: still pending after {stuck_min} min of watching (we only know since first scan). Check fee vs current gas; nothing is changed automatically."))
        res['pending_alerted'] = True
    if res['status'] != 'pending':
        res['pending_alerted'] = False
    for lvl, code, msg in alerts: store.add_alert(tid, lvl, code, msg)
    saved = {**res, 'kind': 'tx', 'address': h, 'chain': chain, 'status_cat': res['status'], 'baseline': prev is None,
             'status': {'tx_status': 'ok' if res['status'] != 'unavailable' else 'error: ' + res['detail']}}
    store.save_snapshot(tid, saved)
    return {'snapshot': saved, 'new_alerts': alerts, 'new_txs': 0}


def gas_scan(client, store, chains):
    """Sample the gas oracle (gwei) per chain; alert when a configured threshold is newly crossed."""
    out = []
    for ch in sorted(set(chains)):
        try:
            g = client.call('get_gas_oracle', {'chainid': ch})
            row = (float(g['SafeGasPrice']), float(g['ProposeGasPrice']), float(g['FastGasPrice']), float(g['suggestBaseFee']), int(g['LastBlock']))
        except Exception as e:  # any failure = unavailable sample, never a fake number
            out.append((ch, None, clean(e, 100))); continue
        store.add_gas(ch, *row)
        out.append((ch, row, ''))
        lo, hi = store.get_setting(f'gas_below_{ch}'), store.get_setting(f'gas_above_{ch}')
        for name, thr, hit in (('below', lo, lo is not None and row[1] <= float(lo or 0)), ('above', hi, hi is not None and row[1] >= float(hi or 0))):
            if thr is None: continue
            key = f'gas_state_{name}_{ch}'
            was = store.get_setting(key) == '1'
            if hit and not was:
                store.add_alert(0, 'info', 'gas_' + name, f'chain {ch}: standard gas {row[1]:.4g} gwei is {name} your {float(thr):g} gwei threshold (Etherscan gas oracle, indicative)')
            store.set_setting(key, '1' if hit else '0')
    return out
