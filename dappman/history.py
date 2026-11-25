"""Bounded historical sync through the keyless Etherscan MCP (free tier: max 10,000 records per query window).
Chucks by block ranges with a persistent checkpoint per target+category, so a run can be stopped and resumed.
Rows are committed page by page and the checkpoint tracks the last FULLY fetched block, so a stop mid-window
loses nothing already stored (re-fetched rows dedup on our primary key).

This is a BOUNDED sync, not a guaranteed-complete history: coverage depends on the provider's index,
rows are deduplicated on our key, reorgs after the fact are not re-checked, and a single block with
more than 10k records in one category is recorded as an explicit gap (the API cannot page past it).
"""
import time
from .client import ApiError

OFFSET = 50
MAX_PAGES = 200           # 200 * 50 = 10,000 = the provider's window cap
MIN_WINDOW = 1            # shrink down to a single block before declaring a gap

TOOLS = {
    'normal': ('get_transactions', {}),
    'internal': ('get_internal_transactions', {}),
    'erc20': ('get_token_transfers', {'standard': 'erc20'}),
    'erc721': ('get_token_transfers', {'standard': 'erc721'}),
    'erc1155': ('get_token_transfers', {'standard': 'erc1155'}),
}
ALL_KINDS = tuple(TOOLS)


def tip_block(client, chain):
    r = client.call('get_block_by_timestamp', {'timestamp': int(time.time()), 'closest': 'before', 'chainid': chain})
    b = r.get('blockNumber') if isinstance(r, dict) else r   # keyless MCP returns the bare block number
    if str(b).isdigit():
        return int(b)
    raise ApiError('tip block unavailable: ' + str(r)[:100])


def sync_target(client, store, target, txrow, kinds=ALL_KINDS, window=500_000, max_calls=None, progress=None):
    """txrow: monitor._tx-style row mapper. Returns per-category report dict. Resumable via settings."""
    tid, chain, addr = target['id'], target['chain'], target['address']
    tip = tip_block(client, chain)
    report = {'chain': chain, 'address': addr, 'tip': tip, 'categories': {}, 'calls': 0}

    def counted(tool, args):
        report['calls'] += 1
        if max_calls and report['calls'] > max_calls:
            raise ApiError('call budget reached (resume later with: dappman sync)')
        return client.call(tool, args)

    for kind in kinds:
        if kind not in TOOLS:
            report['categories'][kind] = {'status': 'skipped: unknown category'}
            continue
        tool, extra = TOOLS[kind]
        key = f'sync_{tid}_{kind}'
        gapkey = f'syncgaps_{tid}_{kind}'
        last = int(store.get_setting(key) or -1)
        if last >= tip:
            report['categories'][kind] = {'status': 'up to date', 'synced_through': last, 'rows': 0}
            continue
        rows_total, capped_windows, windows, pages = 0, 0, 0, 0
        try:
            while last < tip:
                lo, hi = last + 1, min(last + window, tip)
                windows += 1
                args = {'address': addr, 'chainid': chain, 'startblock': lo, 'endblock': hi,
                        'offset': OFFSET, 'sort': 'asc', **extra}
                last_block, capped = None, False
                for pg in range(1, MAX_PAGES + 1):
                    rr = counted(tool, {**args, 'page': pg})
                    pages += 1
                    if rr:
                        rows_total += store.add_txs(tid, [txrow(r, kind) for r in rr])
                        lb = max(int(r.get('blockNumber') or 0) for r in rr)
                        last_block = lb if last_block is None else max(last_block, lb)
                        if last_block > lo:
                            store.set_setting(key, last_block - 1)  # complete through last_block - 1
                    if len(rr) < OFFSET:
                        break
                else:
                    capped = True   # 200 full pages = provider's 10k cap hit inside this window
                if capped:
                    capped_windows += 1
                    if last_block is None or last_block <= lo:
                        gaps = [g for g in (store.get_setting(gapkey) or '').split(',') if g]
                        gaps.append(str(lo))
                        store.set_setting(gapkey, ','.join(gaps))   # one block >10k records: API cannot read it fully
                        store.set_setting(key, lo)
                        last = lo
                        continue
                    window = max((last_block - lo) // 2, MIN_WINDOW)  # shrink, resume from last complete block
                    last = int(store.get_setting(key) or -1)
                    continue
                store.set_setting(key, hi)
                last = hi
                if progress:
                    progress(kind, hi, tip, rows_total)
        except ApiError as e:
            report['categories'][kind] = {'status': 'stopped: ' + str(e)[:120],
                                          'synced_through': int(store.get_setting(key) or -1),
                                          'rows': rows_total, 'pages': pages,
                                          'note': 'partial; all fetched rows are stored; resume with the same command'}
            continue
        st = {'status': 'synced to tip' if last >= tip else 'partial', 'synced_through': last,
              'rows': rows_total, 'windows': windows, 'pages': pages}
        gaps = store.get_setting(gapkey)
        notes = []
        if capped_windows:
            st['capped_windows'] = capped_windows
            notes.append('provider 10k/window cap hit; window shrunk automatically')
        if gaps:
            st['unresolvable_blocks'] = gaps
            notes.append('blocks with >10k same-category records cannot be fully read via this API')
        if notes:
            st['note'] = '; '.join(notes)
        report['categories'][kind] = st
    report['limits'] = ('bounded historical sync via free keyless API: provider index coverage, dedup on our key, '
                        'post-sync reorgs and single blocks above the 10k cap are known limits; not a guaranteed-complete archive')
    return report
