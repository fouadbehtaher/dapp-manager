"""dappman: read-only dApp / wallet / contract monitor.
  dappman add <0xaddr> [--chain 1] [--kind contract|wallet] [--label NAME]
  dappman list | remove <0xaddr> --chain 1
  dappman scan            one scan of all targets (first = baseline, then alerts)
  dappman watch --every 300   loop scan
  dappman alerts [--limit N] [--json]
  dappman tx-status <0xhash> [--chain 1] [--json]   success/failed/pending/not_found/unavailable (exit code 0/3/4/5/6)
  dappman track <0xhash> [--chain 1]   watch a tx: alerts on status change / long pending
  dappman gas [--chain 1] [--json] | gas-alert --chain 1 --below G --above G   (gwei, from Etherscan gas oracle)
  dappman profile [--dapp NAME]
  dappman export --out DIR    CSV of stored rows + alerts
  dappman serve [--port 8787] local dashboard (127.0.0.1)
  python3 -m dappman sync <0xaddr> [--chain 1] [--categories normal,internal,erc20,erc721,erc1155]
                            bounded historical sync (resumable, checkpoints per category)
  dappman history <0xaddr> [--chain 1] [--categories ...] [--limit N] [--json]   stored rows
  dappman verify-traces <0xaddr> [--chain 1] [--max 25]   resolve internal-transfer twins via trace_transaction
  dappman verify-identity <0xaddr> [--chain 1] [--max 100]   resolve NFT event identity from tx receipts
  dappman nfts <0xaddr> [--chains 1,137] [--json]   DERIVED NFT inventory (unconfirmed, see README)
  dappman check-address <0xaddr> [--chain 1] [--json]   OFAC SDN (with list date) + GoPlus signals
  dappman sanctions-update   refresh the cached OFAC SDN crypto-address list
  dappman chains             chains the keyless API currently reports
  dappman mcp-serve          local MCP server on stdio for AI agents (read-only)
Data: SQLite file (--db, default ./dappman.db). No keys, no signing, no writes to chains."""
import argparse, csv, json, os, sys, time
from decimal import Decimal
from .client import McpClient, clean
from .monitor import scan, norm_addr, profile, CHAINS
from .store import Store
from .txstatus import check, norm_hash, gas_scan


def main(argv=None, client=None):
    p = argparse.ArgumentParser(prog='dappman', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', default=os.environ.get('DAPPMAN_DB', 'dappman.db'))
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add'); a.add_argument('address'); a.add_argument('--chain', default='1')
    a.add_argument('--kind', choices=['contract', 'wallet'], default='contract'); a.add_argument('--label', default='')
    a.add_argument('--dapp', default='', help='group several contracts/wallets under one dApp name')
    sub.add_parser('list')
    ts_ = sub.add_parser('tx-status'); ts_.add_argument('hash'); ts_.add_argument('--chain', default='1'); ts_.add_argument('--json', action='store_true')
    tr = sub.add_parser('track'); tr.add_argument('hash'); tr.add_argument('--chain', default='1'); tr.add_argument('--label', default='')
    g = sub.add_parser('gas'); g.add_argument('--chain', default='1'); g.add_argument('--json', action='store_true')
    ga = sub.add_parser('gas-alert'); ga.add_argument('--chain', default='1'); ga.add_argument('--below', type=float); ga.add_argument('--above', type=float)
    pr = sub.add_parser('profile'); pr.add_argument('--dapp', default='')
    r = sub.add_parser('remove'); r.add_argument('address'); r.add_argument('--chain', default='1')
    s = sub.add_parser('scan'); s.add_argument('--big', default='10', help='native-value alert threshold')
    for sp in (s,):
        sp.add_argument('--notify-file', default='', help='append alerts at/above --notify-min to this local JSON-lines file (no network)')
        sp.add_argument('--notify-min', default='warn', choices=['info', 'warn', 'critical'])
    w = sub.add_parser('watch'); w.add_argument('--every', type=int, default=300); w.add_argument('--big', default='10')
    w.add_argument('--notify-file', default=''); w.add_argument('--notify-min', default='warn', choices=['info', 'warn', 'critical'])
    al = sub.add_parser('alerts'); al.add_argument('--limit', type=int, default=30); al.add_argument('--json', action='store_true')
    e = sub.add_parser('export'); e.add_argument('--out', default='export')
    sv = sub.add_parser('serve'); sv.add_argument('--port', type=int, default=8787)
    sy = sub.add_parser('sync'); sy.add_argument('address'); sy.add_argument('--chain', default='1')
    sy.add_argument('--categories', default='normal,internal,erc20,erc721,erc1155')
    sy.add_argument('--window', type=int, default=500000, help='blocks per fetch window (auto-shrinks at the 10k cap)')
    hi = sub.add_parser('history'); hi.add_argument('address'); hi.add_argument('--chain', default='1')
    hi.add_argument('--categories', default='normal,internal,erc20,erc721,erc1155'); hi.add_argument('--limit', type=int, default=50)
    hi.add_argument('--json', action='store_true')
    vi = sub.add_parser('verify-identity'); vi.add_argument('address'); vi.add_argument('--chain', default='1'); vi.add_argument('--max', type=int, default=100)
    vi.add_argument('--all-nft', action='store_true', help='verify every stored NFT tx, not only identity-ambiguous ones'); vi.add_argument('--json', action='store_true')
    vt = sub.add_parser('verify-traces'); vt.add_argument('address'); vt.add_argument('--chain', default='1'); vt.add_argument('--max', type=int, default=25)
    vt.add_argument('--json', action='store_true')
    nf = sub.add_parser('nfts'); nf.add_argument('address'); nf.add_argument('--chains', default='1')
    nf.add_argument('--json', action='store_true')
    nf.add_argument('--confirm', action='store_true', help='confirm candidates on-chain via keyless public RPC (ownerOf/balanceOf at one pinned block)')
    ck = sub.add_parser('check-address'); ck.add_argument('address'); ck.add_argument('--chain', default='1')
    ck.add_argument('--json', action='store_true'); ck.add_argument('--no-refresh', action='store_true')
    sub.add_parser('sanctions-update')
    sub.add_parser('chains')
    sub.add_parser('mcp-serve')
    args = p.parse_args(argv)
    st = Store(args.db)
    mk = (lambda: client) if client else McpClient

    handler = COMMANDS[args.cmd]
    return handler(args, st, mk) or 0


def _cmd_add(args, st, mk):
    addr = norm_addr(args.address)
    tid = st.add_target(args.kind, args.chain, addr, args.label, args.dapp)
    print(f'added #{tid} {args.kind} {addr} chain={args.chain}')
    return 0


def _cmd_tx_status(args, st, mk):
    r_ = check(mk(), args.chain, norm_hash(args.hash))
    print(json.dumps(r_, ensure_ascii=False, indent=1) if args.json else f"{r_['status'].upper()}: {r_['detail']}" + (f" (block {r_['block']})" if r_.get('block') else ''))
    return {'success': 0, 'failed': 3, 'pending': 4, 'not_found': 5, 'unavailable': 6}[r_['status']]  # exit code for scripts
    return 0


def _cmd_track(args, st, mk):
    h_ = norm_hash(args.hash)
    print(f"tracking #{st.add_target('tx', args.chain, h_, args.label)} {h_} chain={args.chain} (status alerts on next scans; read-only)")
    return 0


def _cmd_gas(args, st, mk):
    cl = mk(); r_ = gas_scan(cl, st, [args.chain])[0]
    rows = st.gas_rows(args.chain)
    if r_[1] is None:
        print('gas unavailable:', r_[2]); return 6
    cur = {'chain': args.chain, 'unit': 'gwei', 'safe': r_[1][0], 'standard': r_[1][1], 'fast': r_[1][2], 'base_fee': r_[1][3], 'block': r_[1][4],
           'samples_stored': len(rows), 'min_standard_in_samples': min(x['propose'] for x in rows), 'max_standard_in_samples': max(x['propose'] for x in rows)}
    print(json.dumps(cur, indent=1) if args.json else f"chain {args.chain}: safe {cur['safe']:g} / standard {cur['standard']:g} / fast {cur['fast']:g} gwei, base {cur['base_fee']:g} (block {cur['block']}). {len(rows)} samples stored here (min {cur['min_standard_in_samples']:g}, max {cur['max_standard_in_samples']:g}): only since you started sampling.")
    return 0


def _cmd_gas_alert(args, st, mk):
    if args.below is not None: st.set_setting(f'gas_below_{args.chain}', args.below)
    if args.above is not None: st.set_setting(f'gas_above_{args.chain}', args.above)
    print(f"gas thresholds chain {args.chain}: below={st.get_setting('gas_below_' + args.chain)} above={st.get_setting('gas_above_' + args.chain)} gwei (alerts only; nothing is sent or changed)")
    return 0


def _cmd_list(args, st, mk):
    for t in st.targets():
        print(t['id'], t['kind'], t['chain'], t['address'], t['label'], ('[dapp:' + t['dapp'] + ']') if t.get('dapp') else '')
    return 0


def _cmd_profile(args, st, mk):
    for t in st.targets():
        if t['kind'] != 'contract' or (args.dapp and t.get('dapp') != args.dapp): continue
        p_ = profile(st.window(t['id']), t['address'], verified=bool((st.last_snapshot(t['id'])[1] or {}).get('verified')))
        print(f"[{t['dapp'] or '-'}] {t['label'] or t['address'][:10]}: calls={p_['calls']} failed={p_['failed']} unique_callers={p_['unique_callers']} (stored window only)")
        for m, n in p_['top_methods']: print(f'    {n:4d}  {m}')
        for x in p_['sensitive']: print(f"    ? review {x['why']}: {x['method']}() by {x['caller'][:10]}..." + (' (reverted, no state change expected)' if x['failed'] else ' (name/selector match, not proof of attack)'))
        if p_['unnamed_methods']: print(f"    note: {p_['unnamed_methods']} calls have only a method id (unverified ABI), not checked for sensitive functions")
        if not p_['abi_verified']: print('    note: contract not verified on Etherscan, sensitive-function check is OFF')
    return 0


def _cmd_remove(args, st, mk):
    key = norm_hash(args.address) if len(args.address) == 66 else norm_addr(args.address)
    print('removed' if st.remove_target(args.chain, key) else 'not found')
    return 0


def _cmd_scan(args, st, mk):
    cl = mk()
    from .notify import FileNotifier
    try:
        nf = FileNotifier(args.notify_file, args.notify_min) if args.notify_file else None
    except ValueError as e:
        print('error:', e); return 2
    while True:
        for t in st.targets():
            try:
                res = scan(cl, st, t, Decimal(args.big))
            except Exception as e:  # a watch loop must survive one bad target/network blip
                print(f"[{t['label'] or t['address'][:10]}] scan failed: {clean(e, 100)}"); continue
            sn = res['snapshot']
            bad = {k: v for k, v in sn['status'].items() if v != 'ok'}
            print(f"[{t['label'] or t['address'][:10]}] {'baseline' if sn['baseline'] else 'scan'} new_txs={res['new_txs']} "
                  f"alerts={len(res['new_alerts'])} errors={bad or 'none'}")
            for lvl, code, msg in res['new_alerts']:
                print(f'   ! {lvl} {code}: {msg}')
            if nf:
                try: nf.send(t, res['new_alerts'])
                except OSError as e: print(f'   notify-file write failed: {e}')
        for ch, row, err in gas_scan(cl, st, [t['chain'] for t in st.targets()]):
            print(f"[gas chain {ch}] " + (f"standard {row[1]:g} gwei" if row else 'unavailable: ' + err))
        if args.cmd == 'scan': break
        time.sleep(args.every)
    return 0


def _cmd_alerts(args, st, mk):
    if args.json:
        print(json.dumps(st.alerts(args.limit), ensure_ascii=False, indent=1)); return 0
    for x in st.alerts(args.limit):
        print(time.strftime('%Y-%m-%d %H:%M', time.gmtime(x['ts'])), x['level'], x['label'] or x['address'][:10], x['code'], x['msg'])
    return 0


def _cmd_export(args, st, mk):
    os.makedirs(args.out, exist_ok=True)
    for name, q in (('txs', 'SELECT * FROM txs'), ('alerts', 'SELECT * FROM alerts'), ('targets', 'SELECT * FROM targets')):
        cur = st.db.execute(q)
        with open(os.path.join(args.out, name + '.csv'), 'w', newline='') as f:
            w_ = csv.writer(f); w_.writerow([d[0] for d in cur.description]); w_.writerows(cur.fetchall())
    print('exported to', args.out)
    return 0


def _cmd_serve(args, st, mk):
    import sqlite3
    from .web import serve
    srv = serve(lambda: Store(args.db), mk, port=args.port)
    print(f'dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)')
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    return 0


def _cmd_sync(args, st, mk):
    from .history import sync_target
    addr = norm_addr(args.address)
    tid = st.add_target('wallet', args.chain, addr, args.address[:10], '')  # idempotent; never relabels
    t = [x for x in st.targets() if x['id'] == tid][0]
    from .monitor import _tx
    cats = [c.strip() for c in args.categories.split(',') if c.strip()]
    rep = sync_target(mk(), st, t, _tx, kinds=cats, window=args.window,
                      progress=lambda k, b, tip, n: print(f'  [{k}] block {b}/{tip} ({n} rows new)', flush=True))
    for k, v in rep['categories'].items():
        print(f"{k}: {v['status']} (synced through block {v.get('synced_through', '?')}, {v.get('rows', 0)} new rows)" + (f" | {v['note']}" if v.get('note') else ''))
    print('tip block:', rep['tip'], '| API calls:', rep['calls'])
    print('completeness:', rep['completeness'])
    if rep['legacy_rows_excluded']:
        print(f"note: {rep['legacy_rows_excluded']} pre-migration rows are kept for audit only and are NOT counted")
    print('limits:', rep['limits'])
    return 0


def _cmd_history(args, st, mk):
    addr = norm_addr(args.address)
    t = [x for x in st.targets() if x['address'] == addr and x['chain'] == args.chain]
    if not t:
        print('address is not watched; run: python3 -m dappman sync', addr, '--chain', args.chain); return 2
    cats = [c.strip() for c in args.categories.split(',') if c.strip()]
    rows = [r for r in st.recent_txs(t[0]['id'], 100000) if r['kind'] in cats][:args.limit]
    idm = st.row_identity(t[0]['id'])
    stale = st.stale_checks(t[0]['id'])
    for r in rows:
        r['identity'] = idm.get(r['hash'], 'provider_only') if r['kind'] in ('erc721', 'erc1155') else 'provider_only'
        if r['identity'] == 'receipt_verified' and r['hash'] in stale: r['last_check'] = stale[r['hash']]
    if args.json:
        print(json.dumps({'rows': rows, 'identity_ambiguous_rows': st.ambiguous_rows(t[0]['id']), 'receipts': st.receipt_summary(t[0]['id']),
                          'legacy_rows_excluded': st.legacy_rows(t[0]['id']), 'completeness': 'unproven'}, ensure_ascii=False, indent=1)); return 0
    for r in rows:
        print(time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts'])), r['kind'], r['hash'][:14] + '...',
              (r['frm'] or '')[:10], '->', (r['to_'] or '')[:10], 'value', r['value'], r.get('token') or '', ('FAILED' if r['failed'] else ''), ('[verified EARLIER; LAST CHECK FAILED]' if r.get('last_check') else '[receipt-verified]' if r['identity'] == 'receipt_verified' else '[AMBIGUOUS]' if r['identity'] == 'ambiguous' else ''))
    amb, leg = st.ambiguous_rows(t[0]['id']), st.legacy_rows(t[0]['id'])
    print(f'{len(rows)} stored rows shown (limit {args.limit}). Coverage depends on sync state; this is stored data, not a live query.')
    if amb: print(f'WARNING: {amb} stored rows have byte-identical twins in one tx; the provider gives no log index, so their count is uncertain.')
    if leg: print(f'WARNING: {leg} rows from before the v0.6 migration are excluded; run: python3 -m dappman sync {addr} --chain {args.chain}')
    return 0


def _cmd_verify_traces(args, st, mk):
    from .traces import TraceSource, verify_traces
    addr = norm_addr(args.address)
    t = [x for x in st.targets() if x['address'] == addr and x['chain'] == args.chain]
    if not t:
        print('address is not watched; run: python3 -m dappman sync', addr, '--chain', args.chain); return 2
    tid = t[0]['id']
    hashes = st.trace_hashes(tid); total = len(hashes)
    rep = verify_traces(st, tid, addr, args.chain, TraceSource(args.chain), hashes[:args.max])
    rep.update({'candidates': total, 'not_attempted': total - rep['checked'], 'summary': st.trace_summary(tid),
                'identity_ambiguous_rows': st.ambiguous_rows(tid),
                'completeness': 'unproven: a matching trace fixes internal-transfer identity only for txs the provider returned; endpoint terms and rate limits unreviewed'})
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1)); return 0
    print(f"checked {rep['checked']} of {total} tx (tip {rep['tip']}, host {rep['host']}): " + (', '.join(f'{k}={v}' for k, v in sorted(rep['states'].items())) or 'none'))
    if rep['stopped']:
        print('STOPPED: an endpoint refused the request (' + rep['stopped'] + '). Nothing was retried elsewhere; rerun later.')
    if rep['not_attempted']:
        print(f"{rep['not_attempted']} tx not attempted (limit --max {args.max}); rerun to continue")
    print(f"rows still identity-ambiguous: {rep['identity_ambiguous_rows']}. " + rep['completeness'])
    return 0


def _cmd_verify_identity(args, st, mk):
    from .receipts import ReceiptSource, verify
    addr = norm_addr(args.address)
    t = [x for x in st.targets() if x['address'] == addr and x['chain'] == args.chain]
    if not t:
        print('address is not watched; run: python3 -m dappman sync', addr, '--chain', args.chain); return 2
    tid = t[0]['id']
    hashes = st.ambiguous_hashes(tid, include_all_nft=args.all_nft)
    total = len(hashes)
    hashes = hashes[:args.max]
    src = ReceiptSource(args.chain)
    rep = verify(st, tid, addr, args.chain, src, hashes)
    rep.update({'candidates': total, 'not_attempted': total - rep['checked'], 'summary': st.receipt_summary(tid),
                'identity_ambiguous_rows': st.ambiguous_rows(tid),
                'completeness': 'unproven: receipts fix event identity only for txs the provider returned; txs it omitted are not found'})
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1)); return 0
    print(f"checked {rep['checked']} of {total} tx (tip {rep['tip']}, last host {rep['host']}): " + (', '.join(f'{k}={v}' for k, v in sorted(rep['states'].items())) or 'none'))
    if rep['provider_mismatch']:
        print(f"{len(rep['provider_mismatch'])} tx where the receipt differs from the provider rows (receipt is used for inventory):")
        for m in rep['provider_mismatch'][:10]:
            print(f"   {m['hash'][:14]}... only_in_receipt={m['only_receipt']} only_in_provider={m['only_provider']}")
    if rep['stopped']:
        print('STOPPED: an endpoint refused the request (' + rep['stopped'] + '). Nothing was retried elsewhere; rerun later.')
    if rep['not_attempted']:
        print(f"{rep['not_attempted']} tx not attempted (limit --max {args.max}); rerun to continue")
    print(f"rows still identity-ambiguous: {rep['identity_ambiguous_rows']}. " + rep['completeness'])
    return 0


def _cmd_nfts(args, st, mk):
    from .history import sync_target
    from .nfts import derive
    from .monitor import _tx
    addr = norm_addr(args.address)
    out = []
    for ch in [c.strip() for c in args.chains.split(',') if c.strip()]:
        tid = st.add_target('wallet', ch, addr, args.address[:10], '')
        t = [x for x in st.targets() if x['id'] == tid][0]
        rep = sync_target(mk(), st, t, _tx, kinds=('erc721', 'erc1155'))
        d = derive(st, tid, addr)
        stt = {k: v.get('synced_through') for k, v in rep['categories'].items()}
        out.append({'chain': ch, 'derived_inventory': d['items'], 'derived_from_rows': d['derived_from_rows'],
                    'synced_through': stt, 'sync_status': {k: v['status'] for k, v in rep['categories'].items()},
                    'sync_complete': all(v['status'] in ('synced to tip', 'up to date') for v in rep['categories'].values()),
                    'unconfirmed': True, 'window': d['window'], 'sync_limits': rep['limits'],
                    'identity_ambiguous_rows': d['identity_ambiguous_rows'], 'legacy_rows_excluded': d['legacy_rows_excluded']})
        if args.confirm:
            from .chainrpc import confirm_items
            oc = out[-1]['onchain_confirmation'] = confirm_items(ch, addr, d['items'])
            st.save_confirmations(tid, oc)
            if not oc.get('error'):
                out[-1]['unconfirmed'] = bool(oc['skipped']) or any(r['state'] != 'confirmed_owner' for r in oc['results'])
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1)); return 0
    for ch in out:
        if not ch['sync_complete']:
            print(f"WARNING chain {ch['chain']}: sync NOT complete {ch['sync_status']}; rerun the same command to resume. Candidate list below is partial.")
        print(f"chain {ch['chain']}: {len(ch['derived_inventory'])} candidate NFT item(s), derived from {ch['derived_from_rows']} stored transfers, synced through {ch['synced_through']}")
        oc = ch.get('onchain_confirmation')
        if oc:
            print('   on-chain check (a snapshot AT that block, not current ownership):', oc.get('error') or f"block {oc['block']} via {oc['rpc']}: " + ', '.join(f"{k}={sum(1 for r in oc['results'] if r['state']==k)}" for k in ('confirmed_owner', 'not_owner', 'unknown')) + (f", {oc['skipped']} not checked" if oc['skipped'] else ''))
            for r in oc['results'][:50]:
                print(f"   {r['state']:>15}  #{r['token_id']} {r['contract'][:12]}..." + (f"  ({r['reason']})" if r.get('reason') else ''))
            continue
        for it in ch['derived_inventory'][:50]:
            print(f"   {it['symbol'] or 'NFT'} #{it['token_id']} x{it['qty']}  contract {it['contract'][:12]}...")
    print('DERIVED candidate inventory, NOT confirmed current holdings: transfer events cannot prove ownership (non-standard contracts, indexer gaps, reorgs).')
    print('--confirm checks listed candidates on-chain (ownerOf/balanceOf, one pinned block, public RPC). It does NOT discover')
    print('holdings that the local transfer history lacks.')
    return 0


def _cmd_check_address(args, st, mk):
    from . import sanctions, reputation
    addr = norm_addr(args.address)
    if not args.no_refresh and sanctions.stale(st):
        print('refreshing OFAC SDN list (one-off ~30MB download)...', file=sys.stderr)
        try: print('OFAC list dated', sanctions.refresh(st)['publish_date'], file=sys.stderr)
        except Exception as ex: print('OFAC refresh failed: ' + clean(ex, 100) + ' (check continues with any older cached list)', file=sys.stderr)
    res = {'address': addr, 'chain': args.chain, 'ofac_sdn': sanctions.check(st, addr),
           'goplus': reputation.check(addr, args.chain)}
    loc = [x for x in st.targets() if x['address'] == addr]
    res['local'] = {'watched': bool(loc), 'stored_rows': st.tx_count(loc[0]['id']) if loc else 0,
                    'label': loc[0]['label'] if loc else ''}
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else _fmt_check(res))
    return 0


def _cmd_sanctions_update(args, st, mk):
    from . import sanctions
    r = sanctions.refresh(st)
    print(f"OFAC SDN crypto list cached: {r['addresses']} addresses, list published {r['publish_date']} (source: {sanctions.OFAC_URL})")
    return 0


def _cmd_chains(args, st, mk):
    r = mk().call('get_supported_chains', {})
    rows = r.get('result', r) if isinstance(r, dict) else r
    for c in rows:
        print(f"{c.get('chainid'):>8}  {c.get('chainname')}  ({c.get('comment') or ''})")
    print(f"{len(rows)} chains reported by the keyless API right now. Listing is not a per-chain test.")
    return 0


def _cmd_mcp_serve(args, st, mk):
    from .mcpserver import make_tools, serve_stdio
    serve_stdio(make_tools(lambda: Store(args.db, readonly=True), mk))
    return 0


COMMANDS = {
    'add': _cmd_add,
    'tx-status': _cmd_tx_status,
    'track': _cmd_track,
    'gas': _cmd_gas,
    'gas-alert': _cmd_gas_alert,
    'list': _cmd_list,
    'profile': _cmd_profile,
    'remove': _cmd_remove,
    'scan': _cmd_scan,
    'watch': _cmd_scan,
    'alerts': _cmd_alerts,
    'export': _cmd_export,
    'serve': _cmd_serve,
    'sync': _cmd_sync,
    'history': _cmd_history,
    'nfts': _cmd_nfts,
    'verify-identity': _cmd_verify_identity,
    'verify-traces': _cmd_verify_traces,
    'check-address': _cmd_check_address,
    'sanctions-update': _cmd_sanctions_update,
    'chains': _cmd_chains,
    'mcp-serve': _cmd_mcp_serve,
}


def _fmt_check(res):
    o, g = res['ofac_sdn'], res['goplus']
    lines = [f"address {res['address']} (chain {res['chain']})"]
    if o['state'] == 'listed':
        lines.append(f"OFAC SDN: LISTED as '{o['entity']}' ({'/'.join(o['currencies'])} entry, list dated {o['publish_date']}). A fact about the list, not a legal conclusion.")
    elif o['state'] == 'not_listed':
        lines.append(f"OFAC SDN: not on the list dated {o['publish_date']}. Absence proves nothing.")
    else:
        lines.append('OFAC SDN: unknown (no cached list; run dappman sanctions-update)')
    if g['ok']:
        lines.append(f"GoPlus signals: {', '.join(g['flags']) if g['flags'] else 'none'} (sources: {g['data_source']}). {g['note']}")
    else:
        lines.append('GoPlus signals: ' + g['error'])
    loc = res['local']
    lines.append(f"local: {'watched, ' + str(loc['stored_rows']) + ' stored rows' if loc['watched'] else 'not watched'}")
    return '\n'.join(lines)


if __name__ == '__main__':
    sys.exit(main())
