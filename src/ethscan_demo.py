#!/usr/bin/env python3
"""Read-only wallet report demo via Etherscan KEYLESS MCP (no API key, no account).
Usage: python3 ethscan_demo.py <0x-address> <outdir> [chainid ...]
Outputs: CSVs (full retrieved rows), report.html (Arabic RTL), and per-category
completeness status. Read-only: never signs, sends, or changes anything.
Honesty rules: NFT holdings are INFERRED candidates from transfer history only
(no ownerOf/balanceOf verification). Every category is marked complete or
truncated (with the block reached). API errors are reported, never shown as 0.
"""
import csv, html as htmllib, json, os, sys, time, urllib.request, uuid
from decimal import Decimal

MCP = 'https://mcp-keyless.etherscan.io/mcp'
SID = None
CHAINS = {'1': ('Ethereum', 'ETH'), '137': ('Polygon', 'POL'), '42161': ('Arbitrum One', 'ETH')}
MAX_WINDOW = 10000          # Etherscan V2 returns at most 10k records per query window
PAGE = 100
CALL_BUDGET = int(os.environ.get('CALL_BUDGET', '250'))
CAT_BUDGET = max(10, CALL_BUDGET // 5)
cat_left = CAT_BUDGET
SLEEP = 0.2

calls_used = 0
errors = []  # (chain, category, message)

def rpc(method, params=None, rid=None):
    global SID
    body = {'jsonrpc': '2.0', 'method': method}
    if rid is not None: body['id'] = rid
    if params is not None: body['params'] = params
    req = urllib.request.Request(MCP, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream',
                 **({'Mcp-Session-Id': SID} if SID else {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        if SID is None:
            SID = r.headers.get('mcp-session-id')
        raw = r.read().decode()
    for line in raw.splitlines():
        if line.startswith('data: '):
            return json.loads(line[6:])
    return {}

def init():
    rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'wallet-report-demo', 'version': '0.2'}}, 1)
    rpc('notifications/initialized')

def call(tool, args, count_cat=True):
    """Returns ('ok', result) or ('error', message). Never raises."""
    global calls_used, cat_left
    if calls_used >= CALL_BUDGET or (count_cat and cat_left <= 0):
        return ('budget', None)
    calls_used += 1
    if count_cat: cat_left -= 1
    try:
        d = rpc('tools/call', {'name': tool, 'arguments': args}, uuid.uuid4().int % 10**9)
        payload = json.loads(d['result']['content'][0]['text'])
    except Exception as e:
        return ('error', str(e)[:120])
    if payload.get('ok') and payload.get('status') == '1':
        return ('ok', payload.get('result'))
    msg = payload.get('message') or payload.get('result') or 'unknown'
    if isinstance(msg, str) and 'No transactions found' in msg:
        return ('ok', [])
    return ('error', str(msg)[:120])

def fetch_all(tool, args, keyfn):
    """Complete retrieval: paginate the 10k window desc, then chunk older block
    ranges. Returns (rows, status) where status is 'complete' or
    'truncated@<blockfloor>' or 'error: msg' or 'budget'."""
    seen, rows = set(), []
    start, end = 0, 99999999
    while True:
        got, page, oldest = 0, 1, None
        while got < MAX_WINDOW:
            st, r = call(tool, {**args, 'startblock': start, 'endblock': end,
                                'page': page, 'offset': PAGE, 'sort': 'desc'})
            if st != 'ok':
                return rows, ('error: ' + str(r)) if st == 'error' else 'budget'
            if not r:
                got = None; break
            for t in r:
                k = keyfn(t)
                if k not in seen:
                    seen.add(k); rows.append(t)
                    b = int(t.get('blockNumber', 0))
                    oldest = b if oldest is None else min(oldest, b)
            got += len(r)
            if len(r) < PAGE:
                got = None; break
            page += 1
            time.sleep(SLEEP)
        if got is None:
            return rows, 'complete'          # exhausted this range
        if oldest is None or oldest <= start:
            return rows, 'truncated@window'  # >10k records in a single block
        end = oldest                          # re-query older range (dedup overlaps)
        time.sleep(SLEEP)

def val(t, decimals=18):
    try: return Decimal(t.get('value') or 0) / (Decimal(10) ** decimals)
    except Exception: return Decimal(0)

def ts(t):
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(t.get('timeStamp', 0))))

def main():
    global cat_left
    addr, outdir = sys.argv[1], sys.argv[2]
    chains = sys.argv[3:] or ['1']
    os.makedirs(outdir, exist_ok=True)
    init()
    summ = []   # per chain dict
    for cid in chains:
        cname, sym = CHAINS.get(cid, (f'Chain {cid}', ''))
        st, bal = call('get_native_balance', {'address': addr, 'chainid': cid}, count_cat=False)
        bal_d = (Decimal(bal) / Decimal(10) ** 18) if st == 'ok' else None
        cats = {}
        specs = [
            ('normal', 'get_transactions', {}, lambda t: t.get('hash')),
            ('internal', 'get_internal_transactions', {},
             lambda t: json.dumps(t, sort_keys=True)),
            ('erc20', 'get_token_transfers', {'standard': 'erc20'},
             lambda t: (t.get('hash'), t.get('contractAddress'), t.get('tokenSymbol'))),
            ('erc721', 'get_token_transfers', {'standard': 'erc721'},
             lambda t: (t.get('hash'), t.get('contractAddress'), t.get('tokenID') or t.get('tokenId'))),
            ('erc1155', 'get_token_transfers', {'standard': 'erc1155'},
             lambda t: (t.get('hash'), t.get('contractAddress'), t.get('tokenID') or t.get('tokenId'))),
        ]
        for name, tool, extra, keyfn in specs:
            cat_left = CAT_BUDGET
            rows, status = fetch_all(tool, {'address': addr, 'chainid': cid, **extra}, keyfn)
            cats[name] = (rows, status)
            if rows:
                with open(os.path.join(outdir, f'{name}_{cname.replace(" ", "")}.csv'), 'w', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
                    w.writeheader(); w.writerows(rows)
        # inferred NFT candidates, keyed by chain+contract+tokenID
        a = addr.lower()
        held, nft_status = {}, []
        for std in ('erc721', 'erc1155'):
            rows, status = cats[std]
            nft_status.append(f'{std}: {status} ({len(rows)} تحويل)')
            if not status.startswith('complete'):
                pass
            for t in rows:
                key = (cid, t.get('contractAddress', '?'),
                       t.get('tokenID') or t.get('tokenId') or '?',
                       htmllib.unescape(t.get('tokenName') or '?'), htmllib.unescape(t.get('tokenSymbol') or '?'))
                q = Decimal(t.get('tokenValue') or 1) if std == 'erc1155' else Decimal(1)
                if (t.get('to') or '').lower() == a: held[key] = held.get(key, Decimal(0)) + q
                if (t.get('from') or '').lower() == a: held[key] = held.get(key, Decimal(0)) - q
        cands = {k: v for k, v in held.items() if v > 0}
        nft_complete = all(cats[s][1].startswith('complete') for s in ('erc721', 'erc1155'))
        summ.append({'cid': cid, 'cname': cname, 'sym': sym, 'bal': bal_d,
                     'cats': {n: (len(r), s) for n, (r, s) in cats.items()},
                     'nft_cands': cands, 'nft_complete': nft_complete,
                     'nft_status': nft_status})
    # ---- Arabic RTL HTML summary ----
    def esc(s): return str(s).replace('&', '&amp;').replace('<', '&lt;')
    H = ['<html dir="rtl" lang="ar"><head><meta charset="utf-8">',
         '<style>body{font-family:"Noto Sans Arabic",sans-serif;margin:24px} table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:4px 8px} .mono{direction:ltr;unicode-bidi:embed;font-family:monospace}</style></head><body>',
         f'<h2>تقرير محفظة (قراءة فقط)</h2><p class="mono">{esc(addr)}</p>']
    for s in summ:
        H.append(f'<h3>شبكة {esc(s["cname"])}</h3>')
        H.append(f'<p>الرصيد الأصلي: <b>{s["bal"]:.6f} {s["sym"]}</b></p>' if s['bal'] is not None
                 else '<p>الرصيد الأصلي: <b>غير متاح (خطأ في الجلب)</b></p>')
        H.append('<table><tr><th>الفئة</th><th>السجلات المجلوبة</th><th>الاكتمال</th></tr>')
        labels = {'normal': 'معاملات عادية', 'internal': 'معاملات داخلية',
                  'erc20': 'تحويلات رموز ERC-20', 'erc721': 'تحويلات NFT (ERC-721)',
                  'erc1155': 'تحويلات NFT (ERC-1155)'}
        for n, (cnt, status) in s['cats'].items():
            st_ar = 'مكتمل' if status.startswith('complete') else ('غير مكتمل (تجاوز حد الطلبات في الديمو)' if status == 'budget' else f'غير مكتمل ({esc(status)})')
            H.append(f'<tr><td>{labels[n]}</td><td><span class="mono">{cnt}</span></td><td>{st_ar}</td></tr>')
        H.append('</table>')
        nft_note = 'عدد مرشّح من سجل مكتمل' if s['nft_complete'] else 'تقدير أدنى — السجل غير مكتمل'
        H.append(f'<p>NFTs مرشّحة للملكية: <b><span class="mono">{len(s["nft_cands"])}</span></b> قطعة — {nft_note}. تنبيه: العدد استنتاج من سجل التحويلات فقط بدون تحقق ownerOf/balanceOf، يعني مش عدد مؤكد للملكية الحالية.</p>')
        if s['nft_cands']:
            H.append('<ul>')
            for (c, ca, tid, n, sy), v in list(s['nft_cands'].items())[:15]:
                qty = f' ×{v}' if v > 1 else ''
                H.append(f'<li>{esc(n)} ({esc(sy)}) #{esc(tid)}{qty} <span class="mono">{esc(ca[:12])}…</span></li>')
            H.append('</ul>')
    H.append(f'<p><small>استُهلك {calls_used} طلب. ديمو قراءة فقط عبر Etherscan Keyless MCP. لا مفاتيح حساب، لا توقيع، لا معاملات. البيانات الكاملة في ملفات CSV.</small></p></body></html>')
    with open(os.path.join(outdir, 'report.html'), 'w') as f:
        f.write('\n'.join(H))
    out = {'address': addr, 'chains': [{'cid': s['cid'], 'cname': s['cname'], 'sym': s['sym'],
            'balance_native': str(s['bal']) if s['bal'] is not None else None,
            'cats': s['cats']} for s in summ]}
    with open(os.path.join(outdir, 'summary.json'), 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({'calls': calls_used, 'summary': [
        {'chain': s['cname'], 'cats': s['cats'], 'nft_candidates': len(s['nft_cands']),
         'nft_history_complete': s['nft_complete']} for s in summ]}, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
