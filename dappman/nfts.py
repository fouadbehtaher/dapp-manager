"""Derived NFT INVENTORY from stored ERC-721/1155 transfer rows (transfers in minus out).

IMPORTANT: this is a derived CANDIDATE inventory, not confirmed current holdings. Transfers alone cannot
prove ownership: non-standard contracts that skip events, indexer gaps, reorgs, and anything missed by a
partial sync all change the real answer. Confirming actual ownership needs an on-chain ownerOf/balanceOf read
or a verified holdings provider (both need a keyed/paid API the keyless tool does not have). Say 'derived
inventory, unconfirmed', never 'holdings'.
"""
ZERO = '0x' + '0' * 40


def derive(store, tid, addr):
    """Returns {'items': [...], 'derived_from_rows': n, 'window': note} over whatever the store actually holds."""
    addr = (addr or '').lower()
    bal = {}   # (chain-agnostic per target) (contract, token_id) -> [qty, symbol]
    n_rows = 0
    for r in store.nft_rows(tid):
        n_rows += 1
        key = (r['contract'].lower(), r['token_id'])
        qty, sym, _k = bal.get(key, [0, (r['token'] or '').split('|')[0][:20], r['kind']])
        amt = 1 if r['kind'] == 'erc721' else _int(r['value'], 1)
        if (r['to_'] or '').lower() == addr:
            qty += amt
        if (r['frm'] or '').lower() == addr:
            qty -= amt
        bal[key] = [qty, sym, r['kind']]
    items = [{'contract': c, 'token_id': i, 'qty': q, 'symbol': s, 'kind': k}
             for (c, i), (q, s, k) in sorted(bal.items()) if q > 0]
    negative = sum(1 for v in bal.values() if v[0] < 0)  # more out than in: history incomplete for these
    legacy, amb = store.legacy_rows(tid), store.ambiguous_rows(tid)
    return {'items': items, 'derived_from_rows': n_rows, 'inconsistent_tokens': negative,
            'legacy_rows_excluded': legacy, 'identity_ambiguous_rows': amb,
            'window': 'derived from stored transfers only; unconfirmed: not current ownership (missing/non-standard events, '
                      'partial sync, reorgs, ERC-1155 batch edge cases, burns/mints not independently checked)'
                      + (f'; {legacy} pre-migration rows are excluded until `python3 -m dappman sync` re-fetches them' if legacy else '')
                      + (f'; {amb} rows have byte-identical twins in one tx and the provider gives no log index, so their count is uncertain' if amb else '')
                      + (f'; {negative} token(s) show more out than in, so history is incomplete for them' if negative else '')}


def _int(v, default):
    try: return int(v)
    except Exception: return default
