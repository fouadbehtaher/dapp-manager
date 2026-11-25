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
  dappman sync <0xaddr> [--chain 1] [--categories normal,internal,erc20,erc721,erc1155]
                            bounded historical sync (resumable, checkpoints per category)
  dappman history <0xaddr> [--chain 1] [--categories ...] [--limit N] [--json]   stored rows
  dappman nfts <0xaddr> [--chains 1,137] [--json]   DERIVED NFT inventory (unconfirmed, see README)
  dappman check-address <0xaddr> [--chain 1] [--json]   OFAC SDN (with list date) + GoPlus signals
  dappman sanctions-update   refresh the cached OFAC SDN crypto-address list
  dappman chains             chains the keyless API currently reports
  dappman mcp-serve          local MCP server on stdio for AI agents (read-only)
Data: SQLite file (--db, default ./dappman.db). No keys, no signing, no writes to chains."""
import argparse, csv, json, os, sys, time
from decimal import Decimal
from .client import McpClient
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
    w = sub.add_parser('watch'); w.add_argument('--every', type=int, default=300); w.add_argument('--big', default='10')
    al = sub.add_parser('alerts'); al.add_argument('--limit', type=int, default=30); al.add_argument('--json', action='store_true')
    e = sub.add_parser('export'); e.add_argument('--out', default='export')
    sv = sub.add_parser('serve'); sv.add_argument('--port', type=int, default=8787)
    sy = sub.add_parser('sync'); sy.add_argument('address'); sy.add_argument('--chain', default='1')
    sy.add_argument('--categories', default='normal,internal,erc20,erc721,erc1155')
    sy.add_argument('--window', type=int, default=500000, help='blocks per fetch window (auto-shrinks at the 10k cap)')
    hi = sub.add_parser('history'); hi.add_argument('address'); hi.add_argument('--chain', default='1')
    hi.add_argument('--categories', default='normal,internal,erc20,erc721,erc1155'); hi.add_argument('--limit', type=int, default=50)
    hi.add_argument('--json', action='store_true')
    nf = sub.add_parser('nfts'); nf.add_argument('address'); nf.add_argument('--chains', default='1')
    nf.add_argument('--json', action='store_true')
    ck = sub.add_parser('check-address'); ck.add_argument('address'); ck.add_argument('--chain', default='1')
    ck.add_argument('--json', action='store_true'); ck.add_argument('--no-refresh', action='store_true')
    sub.add_parser('sanctions-update')
    sub.add_parser('chains')
    sub.add_parser('mcp-serve')
    args = p.parse_args(argv)
    st = Store(args.db)
    mk = (lambda: client) if client else McpClient

    if args.cmd == 'add':
        addr = norm_addr(args.address)
        tid = st.add_target(args.kind, args.chain, addr, args.label, args.dapp)
        print(f'added #{tid} {args.kind} {addr} chain={args.chain}')
    elif args.cmd == 'tx-status':
        r_ = check(mk(), args.chain, norm_hash(args.hash))
        print(json.dumps(r_, ensure_ascii=False, indent=1) if args.json else f"{r_['status'].upper()}: {r_['detail']}" + (f" (block {r_['block']})" if r_.get('block') else ''))
        return {'success': 0, 'failed': 3, 'pending': 4, 'not_found': 5, 'unavailable': 6}[r_['status']]  # exit code for scripts
    elif args.cmd == 'track':
        h_ = norm_hash(args.hash)
        print(f"tracking #{st.add_target('tx', args.chain, h_, args.label)} {h_} chain={args.chain} (status alerts on next scans; read-only)")
    elif args.cmd == 'gas':
        cl = mk(); r_ = gas_scan(cl, st, [args.chain])[0]
        rows = st.gas_rows(args.chain)
        if r_[1] is None:
            print('gas unavailable:', r_[2]); return 6
        cur = {'chain': args.chain, 'unit': 'gwei', 'safe': r_[1][0], 'standard': r_[1][1], 'fast': r_[1][2], 'base_fee': r_[1][3], 'block': r_[1][4],
               'samples_stored': len(rows), 'min_standard_in_samples': min(x['propose'] for x in rows), 'max_standard_in_samples': max(x['propose'] for x in rows)}
        print(json.dumps(cur, indent=1) if args.json else f"chain {args.chain}: safe {cur['safe']:g} / standard {cur['standard']:g} / fast {cur['fast']:g} gwei, base {cur['base_fee']:g} (block {cur['block']}). {len(rows)} samples stored here (min {cur['min_standard_in_samples']:g}, max {cur['max_standard_in_samples']:g}): only since you started sampling.")
    elif args.cmd == 'gas-alert':
        if args.below is not None: st.set_setting(f'gas_below_{args.chain}', args.below)
        if args.above is not None: st.set_setting(f'gas_above_{args.chain}', args.above)
        print(f"gas thresholds chain {args.chain}: below={st.get_setting('gas_below_' + args.chain)} above={st.get_setting('gas_above_' + args.chain)} gwei (alerts only; nothing is sent or changed)")
    elif args.cmd == 'list':
        for t in st.targets():
            print(t['id'], t['kind'], t['chain'], t['address'], t['label'], ('[dapp:' + t['dapp'] + ']') if t.get('dapp') else '')
    elif args.cmd == 'profile':
        for t in st.targets():
            if t['kind'] != 'contract' or (args.dapp and t.get('dapp') != args.dapp): continue
            p_ = profile(st.window(t['id']), t['address'], verified=bool((st.last_snapshot(t['id'])[1] or {}).get('verified')))
            print(f"[{t['dapp'] or '-'}] {t['label'] or t['address'][:10]}: calls={p_['calls']} failed={p_['failed']} unique_callers={p_['unique_callers']} (stored window only)")
            for m, n in p_['top_methods']: print(f'    {n:4d}  {m}')
            for x in p_['sensitive']: print(f"    ? review {x['why']}: {x['method']}() by {x['caller'][:10]}..." + (' (reverted, no state change expected)' if x['failed'] else ' (name/selector match, not proof of attack)'))
            if p_['unnamed_methods']: print(f"    note: {p_['unnamed_methods']} calls have only a method id (unverified ABI), not checked for sensitive functions")
            if not p_['abi_verified']: print('    note: contract not verified on Etherscan, sensitive-function check is OFF')
    elif args.cmd == 'remove':
        key = norm_hash(args.address) if len(args.address) == 66 else norm_addr(args.address)
        print('removed' if st.remove_target(args.chain, key) else 'not found')
    elif args.cmd in ('scan', 'watch'):
        cl = mk()
        while True:
            for t in st.targets():
                try:
                    res = scan(cl, st, t, Decimal(args.big))
                except Exception as e:  # a watch loop must survive one bad target/network blip
                    print(f"[{t['label'] or t['address'][:10]}] scan failed: {str(e)[:100]}"); continue
                sn = res['snapshot']
                bad = {k: v for k, v in sn['status'].items() if v != 'ok'}
                print(f"[{t['label'] or t['address'][:10]}] {'baseline' if sn['baseline'] else 'scan'} new_txs={res['new_txs']} "
                      f"alerts={len(res['new_alerts'])} errors={bad or 'none'}")
                for lvl, code, msg in res['new_alerts']:
                    print(f'   ! {lvl} {code}: {msg}')
            for ch, row, err in gas_scan(cl, st, [t['chain'] for t in st.targets()]):
                print(f"[gas chain {ch}] " + (f"standard {row[1]:g} gwei" if row else 'unavailable: ' + err))
            if args.cmd == 'scan': break
            time.sleep(args.every)
    elif args.cmd == 'alerts':
        if args.json:
            print(json.dumps(st.alerts(args.limit), ensure_ascii=False, indent=1)); return 0
        for x in st.alerts(args.limit):
            print(time.strftime('%Y-%m-%d %H:%M', time.gmtime(x['ts'])), x['level'], x['label'] or x['address'][:10], x['code'], x['msg'])
    elif args.cmd == 'export':
        os.makedirs(args.out, exist_ok=True)
        for name, q in (('txs', 'SELECT * FROM txs'), ('alerts', 'SELECT * FROM alerts'), ('targets', 'SELECT * FROM targets')):
            cur = st.db.execute(q)
            with open(os.path.join(args.out, name + '.csv'), 'w', newline='') as f:
                w_ = csv.writer(f); w_.writerow([d[0] for d in cur.description]); w_.writerows(cur.fetchall())
        print('exported to', args.out)
    elif args.cmd == 'serve':
        import sqlite3
        from .web import serve
        srv = serve(lambda: Store(args.db), mk, port=args.port)
        print(f'dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)')
        try: srv.serve_forever()
        except KeyboardInterrupt: pass
    elif args.cmd == 'sync':
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
        print('limits:', rep['limits'])
    elif args.cmd == 'history':
        addr = norm_addr(args.address)
        t = [x for x in st.targets() if x['address'] == addr and x['chain'] == args.chain]
        if not t:
            print('address is not watched; run: dappman sync', addr, '--chain', args.chain); return 2
        cats = [c.strip() for c in args.categories.split(',') if c.strip()]
        rows = [r for r in st.recent_txs(t[0]['id'], 100000) if r['kind'] in cats][:args.limit]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1)); return 0
        for r in rows:
            print(time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts'])), r['kind'], r['hash'][:14] + '...',
                  (r['frm'] or '')[:10], '->', (r['to_'] or '')[:10], 'value', r['value'], r.get('token') or '', ('FAILED' if r['failed'] else ''))
        print(f'{len(rows)} stored rows shown (limit {args.limit}). Coverage depends on sync state; this is stored data, not a live query.')
    elif args.cmd == 'nfts':
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
                        'synced_through': stt, 'unconfirmed': True, 'window': d['window'], 'sync_limits': rep['limits']})
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1)); return 0
        for ch in out:
            print(f"chain {ch['chain']}: {len(ch['derived_inventory'])} candidate NFT item(s), derived from {ch['derived_from_rows']} stored transfers, synced through {ch['synced_through']}")
            for it in ch['derived_inventory'][:50]:
                print(f"   {it['symbol'] or 'NFT'} #{it['token_id']} x{it['qty']}  contract {it['contract'][:12]}...")
        print('DERIVED candidate inventory, NOT confirmed current holdings: transfer events cannot prove ownership')
        print('(non-standard contracts, indexer gaps, reorgs). Confirming ownership needs an on-chain ownerOf/balanceOf read')
        print('or a verified holdings provider (keyed/paid API, not included).')
    elif args.cmd == 'check-address':
        from . import sanctions, reputation
        addr = norm_addr(args.address)
        if not args.no_refresh and sanctions.stale(st):
            print('refreshing OFAC SDN list (one-off ~30MB download)...', file=sys.stderr)
            try: print('OFAC list dated', sanctions.refresh(st)['publish_date'], file=sys.stderr)
            except Exception as ex: print('OFAC refresh failed: ' + str(ex)[:100] + ' (check continues with any older cached list)', file=sys.stderr)
        res = {'address': addr, 'chain': args.chain, 'ofac_sdn': sanctions.check(st, addr),
               'goplus': reputation.check(addr, args.chain)}
        loc = [x for x in st.targets() if x['address'] == addr]
        res['local'] = {'watched': bool(loc), 'stored_rows': st.tx_count(loc[0]['id']) if loc else 0,
                        'label': loc[0]['label'] if loc else ''}
        print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else _fmt_check(res))
    elif args.cmd == 'sanctions-update':
        from . import sanctions
        r = sanctions.refresh(st)
        print(f"OFAC SDN crypto list cached: {r['addresses']} addresses, list published {r['publish_date']} (source: {sanctions.OFAC_URL})")
    elif args.cmd == 'chains':
        r = mk().call('get_supported_chains', {})
        rows = r.get('result', r) if isinstance(r, dict) else r
        for c in rows:
            print(f"{c.get('chainid'):>8}  {c.get('chainname')}  ({c.get('comment') or ''})")
        print(f"{len(rows)} chains reported by the keyless API right now. Listing is not a per-chain test.")
    elif args.cmd == 'mcp-serve':
        from .mcpserver import make_tools, serve_stdio
        serve_stdio(make_tools(lambda: Store(args.db, readonly=True), mk))
    return 0


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
