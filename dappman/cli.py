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
    return 0


if __name__ == '__main__':
    sys.exit(main())
