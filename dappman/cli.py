"""dappman: read-only dApp / wallet / contract monitor.
  dappman add <0xaddr> [--chain 1] [--kind contract|wallet] [--label NAME]
  dappman list | remove <0xaddr> --chain 1
  dappman scan            one scan of all targets (first = baseline, then alerts)
  dappman watch --every 300   loop scan
  dappman alerts [--limit N]
  dappman export --out DIR    CSV of stored rows + alerts
  dappman serve [--port 8787] local dashboard (127.0.0.1)
Data: SQLite file (--db, default ./dappman.db). No keys, no signing, no writes to chains."""
import argparse, csv, json, os, sys, time
from decimal import Decimal
from .client import McpClient
from .monitor import scan, norm_addr, CHAINS
from .store import Store


def main(argv=None, client=None):
    p = argparse.ArgumentParser(prog='dappman', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', default=os.environ.get('DAPPMAN_DB', 'dappman.db'))
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add'); a.add_argument('address'); a.add_argument('--chain', default='1')
    a.add_argument('--kind', choices=['contract', 'wallet'], default='contract'); a.add_argument('--label', default='')
    sub.add_parser('list')
    r = sub.add_parser('remove'); r.add_argument('address'); r.add_argument('--chain', default='1')
    s = sub.add_parser('scan'); s.add_argument('--big', default='10', help='native-value alert threshold')
    w = sub.add_parser('watch'); w.add_argument('--every', type=int, default=300); w.add_argument('--big', default='10')
    al = sub.add_parser('alerts'); al.add_argument('--limit', type=int, default=30)
    e = sub.add_parser('export'); e.add_argument('--out', default='export')
    sv = sub.add_parser('serve'); sv.add_argument('--port', type=int, default=8787)
    args = p.parse_args(argv)
    st = Store(args.db)
    mk = (lambda: client) if client else McpClient

    if args.cmd == 'add':
        addr = norm_addr(args.address)
        tid = st.add_target(args.kind, args.chain, addr, args.label)
        print(f'added #{tid} {args.kind} {addr} chain={args.chain}')
    elif args.cmd == 'list':
        for t in st.targets():
            print(t['id'], t['kind'], t['chain'], t['address'], t['label'])
    elif args.cmd == 'remove':
        print('removed' if st.remove_target(args.chain, norm_addr(args.address)) else 'not found')
    elif args.cmd in ('scan', 'watch'):
        cl = mk()
        while True:
            for t in st.targets():
                res = scan(cl, st, t, Decimal(args.big))
                sn = res['snapshot']
                bad = {k: v for k, v in sn['status'].items() if v != 'ok'}
                print(f"[{t['label'] or t['address'][:10]}] {'baseline' if sn['baseline'] else 'scan'} new_txs={res['new_txs']} "
                      f"alerts={len(res['new_alerts'])} errors={bad or 'none'}")
                for lvl, code, msg in res['new_alerts']:
                    print(f'   ! {lvl} {code}: {msg}')
            if args.cmd == 'scan': break
            time.sleep(args.every)
    elif args.cmd == 'alerts':
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
