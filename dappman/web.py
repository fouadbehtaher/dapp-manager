"""Local dashboard (127.0.0.1 only). Reads the SQLite store; POST /api/scan re-scans watched targets."""
import json, threading, time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .monitor import scan, scan_all, eth, fmt, profile, CHAINS
import secrets

PAGE = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>dApp Manager</title>
<style nonce="__N__">
*{box-sizing:border-box}body{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;margin:0;background:#0f1420;color:#e8ecf4}
header{padding:10px 12px;background:#161d2e;display:flex;gap:8px;align-items:center;position:sticky;top:0;z-index:2}
h1{font-size:15px;margin:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:8px 12px;font-size:13px;white-space:nowrap}button:disabled{opacity:.5}
main{padding:10px;max-width:900px;margin:auto}.card{background:#161d2e;border-radius:12px;padding:12px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;align-items:center}.muted{color:#93a0b8;font-size:12px;line-height:1.7}
.mono{font-family:ui-monospace,Menlo,monospace;direction:ltr;unicode-bidi:isolate;word-break:break-all;font-size:11px;text-align:left}
.tag{border-radius:6px;padding:2px 7px;font-size:11px;margin-inline-end:4px;display:inline-block;white-space:nowrap}
.info{background:#1e3a5f}.warn{background:#7a5a00}.critical{background:#8f2323}.ok{background:#14532d;color:#d7f5df}.err{background:#8f2323}
.tx{border-top:1px solid #26304a;padding:7px 0;display:grid;grid-template-columns:1fr auto;gap:2px 8px;font-size:12px}
.val{direction:ltr;unicode-bidi:isolate;font-weight:600;text-align:left;overflow-wrap:anywhere;min-width:0}
h2{font-size:14px;margin:8px 0 2px}.al{padding:5px 0;border-top:1px solid #26304a;font-size:12.5px;line-height:1.6}
.ltr{direction:ltr;unicode-bidi:isolate;display:inline-block;max-width:100%;overflow-wrap:anywhere;word-break:break-all}.tx>*{min-width:0;overflow-wrap:anywhere}.nft{max-height:none}
</style></head><body><header><h1>مدير التطبيقات اللامركزية</h1><span class="tag ok">قراءة فقط</span><button id="b">فحص الآن</button></header>
<main><div id="alerts"></div><div id="gas"></div><div id="targets"></div>
<div class="card muted"><b>دليل الرموز:</b> <span class="tag ok">✓ يعمل</span> الفئة اتقرأت بنجاح · <span class="tag err">✗ خطأ</span> فشل القراءة (مش صفر، مجهول) ·
الفئات: الرصيد / الإنشاء / الكود المصدري / المعاملات / المعاملات الداخلية / توكن ERC-20 / NFT 721 / NFT 1155. فحص الأطراف المقابلة مقابل قائمة OFAC يعمل فقط بعد تحميلها (sanctions-update) ويظهر تاريخ القائمة المستخدم.<br>
<b>الوحدات:</b> "ETH/POL" = العملة الأصلية للشبكة بعد القسمة على 10^18، وليست دولارًا. قيم التوكن = المبلغ الخام ÷ 10^(decimals التوكن) بدون أي سعر. اسم التوكن يكتبه صاحبه وقد يكون مضللًا.<br>
<b>التغطية:</b> أول فحص = آخر ٥٠ سجلًا لكل فئة. الفحوص التالية ترجع صفحات حتى آخر سجل معروف (حد أقصى ٢٥٠). لو تجاوز الحد يظهر تنبيه "فجوة تغطية". ليست سجلًا كاملًا، وليست تدقيقًا أمنيًا.<br><b>أولوية المراجعة:</b> تطابق اسم دالة مع selector المعاملة (فحص اتساق، ليس تحقق ABI، وتصادم selectors ممكن)، لمعاملات مباشرة إلى العقد فقط؛ ليس إثبات هجوم ولا تغيّر حالة. المعاملات الداخلية تُجمع وتُعرض كسجل تاريخي فقط؛ تحليل أسماء الدوال للاستدعاءات الداخلية/عبر multisig أو router غير مدعوم، فلا تنبيه sensitive_call لها. <b>مخزون NFT:</b> مرشح مشتق من التحويلات، ليس ملكية حالية مؤكدة. <b>OFAC:</b> حقيقة عن القائمة بتاريخها وليست حكمًا قانونيًا، وقائمة قديمة تعني «مجهول».</div></main></main>
<script nonce="__N__">
const E=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const D=t=>t?new Date(t*1000).toISOString().replace('T',' ').slice(0,16)+' UTC':'—';
const CAT={balance:'الرصيد',creation:'الإنشاء',source:'الكود',transactions:'المعاملات',internal_transactions:'المعاملات الداخلية',token_transfers:'توكن ERC-20',nft_721:'NFT 721',nft_1155:'NFT 1155'};
const KIND={normal:'عادية',internal:'داخلية',erc20:'توكن',erc721:'NFT',erc1155:'NFT'};
const LV={info:'معلومة',warn:'تحذير',critical:'حرج'};
const S=a=>E(a.slice(0,8))+'…'+E(a.slice(-4));
async function load(){const r=await fetch('/api/state');const s=await r.json();
document.getElementById('alerts').innerHTML='<div class="card"><h2>التنبيهات</h2>'+(s.alerts.length?s.alerts.map(a=>
`<div class="al"><span class="tag ${E(a.level)}">${E(LV[a.level]||a.level)}</span><b>${E(a.label||a.address.slice(0,10))}</b>: <span class="ltr">${E(a.msg)}</span><div class="muted">${D(a.ts)}</div></div>`).join(''):'<span class="muted">لا تنبيهات. تبدأ من الفحص الثاني بعد تسجيل خط الأساس.</span>')+'</div>';
const TXL={success:'نجحت',failed:'فشلت (ارتدّت)',pending:'معلّقة',not_found:'غير موجودة عند Etherscan',unavailable:'الحالة غير متاحة'};
document.getElementById('gas').innerHTML=s.gas.length?'<div class="card"><h2>سعر الغاز (gwei، من Etherscan، إرشادي)</h2>'+s.gas.map(g=>`<div class="al"><b>${E(g.chain_name)}</b>: <span class="ltr">آمن ${E(g.latest.safe.toPrecision(3))} · عادي ${E(g.latest.propose.toPrecision(3))} · سريع ${E(g.latest.fast.toPrecision(3))} gwei</span><div class="muted">${g.n} عينة محفوظة فقط منذ بدء المراقبة (أدنى ${E(g.min.toPrecision(3))} · أعلى ${E(g.max.toPrecision(3))}) · آخر عينة ${D(g.latest.ts)}${g.below!==null||g.above!==null?' · عتبات التنبيه: '+E(g.below??'—')+' / '+E(g.above??'—')+' gwei':''}</div></div>`).join('')+'</div>':'';
document.getElementById('targets').innerHTML=s.targets.map(t=>{const n=t.snapshot||{};const st=n.status||{};
if(t.kind==='tx'){const c=n.status_cat;return `<div class="card"><div class="row"><b>${E(t.label||'معاملة')}</b><span><span class="tag info">معاملة</span><span class="tag info">${E(t.chain_name)}</span></span></div><div class="mono">${E(t.address)}</div><div><span class="tag ${c==='success'?'ok':c==='failed'||c==='unavailable'?'err':'warn'}">${E(TXL[c]||'—')}</span></div><div class="muted">${E(n.detail||'')}${n.block?' · بلوك '+E(n.block):''}<br>آخر فحص: ${D(t.scanned_at)} · لا يُغيَّر شيء تلقائيًا، قراءة فقط.</div></div>`}
const gap=(n.gaps||[]).length?`<div class="tag warn">فجوة تغطية محتملة: ${E(n.gaps.join(', '))}</div>`:'';
return `<div class="card"><div class="row"><b>${t.dapp?'<span class="tag info">dApp: '+E(t.dapp)+'</span>':''}${E(t.label||t.address.slice(0,10))}</b><span><span class="tag info">${E(t.kind==='contract'?'عقد':'محفظة')}</span><span class="tag info">${E(t.chain_name)}</span></span></div>
<div class="mono">${E(t.address)}</div>
<div class="muted">آخر فحص: ${D(t.scanned_at)} · الرصيد: <span class="ltr">${n.native_balance?E(t.balance_fmt)+' '+E(t.sym):'—'}</span> · سجلات محفوظة: ${t.tx_count}</div>
${t.kind==='contract'?`<div class="muted">الاسم: <span class="ltr">${E(n.name||'—')}</span> · الكود: ${n.verified===undefined?'—':n.verified?'<span class="tag ok">موثّق</span>':'<span class="tag err">غير موثّق</span>'}${n.proxy?' · وكيل → <span class="mono">'+E(n.implementation)+'</span>':''}</div>`:''}
${n.kind==='contract'||t.kind==='contract'?`<div class="muted">نشاط النافذة المحفوظة فقط: ${t.profile.calls} استدعاء · فشل ${t.profile.failed} · ${t.profile.unique_callers} مستدعي مختلف${t.profile.top_methods.length?'<br>أكثر الدوال: <span class="ltr">'+t.profile.top_methods.slice(0,3).map(m=>E(m[0])+' ×'+m[1]).join(' · ')+'</span>':''}${t.profile.unnamed_methods?'<br>'+t.profile.unnamed_methods+' استدعاء بدون اسم دالة (ABI غير موثّق) ولم يُفحص للدوال الحساسة':''}</div>`+t.profile.sensitive.map(x=>`<div class="al"><span class="tag ${E(x.level)}">${E(LV[x.level])}</span>أولوية مراجعة (تطابق اسم الدالة فقط، ليس إثبات هجوم): <span class="ltr">${E(x.method)}() — ${E(x.why)}</span> ${x.failed?'(فشلت/ارتدّت، لا تغيّر حالة متوقع)':''}<div class="muted">${D(x.ts)} · <span class="mono">${S(x.caller)}</span></div></div>`).join(''):''}
<div>${Object.entries(st).map(([k,v])=>`<span class="tag ${v==='ok'?'ok':'err'}" title="${E(v)}">${v==='ok'?'✓':'✗'} ${E(CAT[k]||k)}</span>`).join('')}${gap}</div>
${t.sanctions_note?'<div class="muted">'+E(t.sanctions_note)+'</div>':''}${t.sanctions_date?'<div class="muted">فحص الأطراف المقابلة مقابل قائمة OFAC بتاريخ '+E(t.sanctions_date)+'</div>':''}
${t.nft?`<h2>مخزون NFT مشتق (غير مؤكد)</h2><div class="muted">${t.nft.total} عنصرًا مرشحًا من ${t.nft.rows} تحويلًا محفوظًا — التحويلات وحدها لا تثبت الملكية الحالية (عقود غير قياسية، فجوات الفهرسة، إعادة تنظيم السلسلة). التأكيد يحتاج قراءة ownerOf/balanceOf على السلسلة أو مزوّد ملكية موثّق.</div>`+t.nft.items.slice(0,10).map(i=>{const id=String(i.token_id);const sid=id.length>14?id.slice(0,6)+'…'+id.slice(-6):id;return `<div class="tx"><span class="ltr" title="${E(id)}">${E(i.symbol||'NFT')} #${E(sid)} ×${E(i.qty)}</span><span class="mono">${S(i.contract)}</span></div>`}).join('')+(t.nft.total>10?`<div class="muted">+${t.nft.total-10} عنصرًا آخر غير معروض (الأول 10 فقط). التصدير الكامل: dappman nfts --json</div>`:'')+(t.nft.inconsistent?`<div class="muted">${t.nft.inconsistent} توكن خارج أكثر من داخل: السجل ناقص لها.</div>`:''):''}
<h2>آخر المعاملات</h2>`+t.recent.map(x=>`<div class="tx"><span>${D(x.ts)} · ${E(KIND[x.kind]||x.kind)}${x.failed?' <span class="tag err">فشلت</span>':''}</span><span class="val">${E(x.display)}</span><span class="mono">${S(x.frm)} → ${S(x.to_||'')}</span><span></span></div>`).join('')+`</div>`}).join('')||'<div class="card muted">لا أهداف. أضف عنوانًا بالأمر dappman add.</div>';}
async function scanNow(){const b=document.getElementById('b');b.disabled=true;b.textContent='جارٍ الفحص…';
try{await fetch('/api/scan',{method:'POST',headers:{'X-Dappman':'1'}});}finally{b.disabled=false;b.textContent='فحص الآن';load();}}
document.getElementById('b').addEventListener('click',scanNow);load();setInterval(load,30000);
</script></body></html>"""


def _short_id(v, n=6):
    v = str(v)
    return v if len(v) <= 2 * n + 2 else v[:n] + '…' + v[-n:]


def state(store):
    out = []
    for t in store.targets():
        ts, snap = store.last_snapshot(t['id'])
        name, sym = CHAINS.get(t['chain'], ('Chain ' + t['chain'], ''))
        rec = []
        for r in store.recent_txs(t['id'], 15):
            if r['kind'] == 'erc20':
                dec = int((r['token'].split('|') + ['18'])[1] or 18)
                disp = f"{fmt(eth(r['value'], dec))} {(r['token'].split('|')[0] or 'token')}"
            elif r['kind'] == 'erc721':
                disp = f"{(r['token'] or 'NFT')} #{_short_id(r['token_id'])}"
            elif r['kind'] == 'erc1155':
                disp = f"x{r['value']} {(r['token'] or 'NFT')} #{_short_id(r['token_id'])}"
            elif r['kind'] == 'internal':
                disp = f"{fmt(eth(r['value']))} {sym} (داخلية)"
            else:
                disp = f"{fmt(eth(r['value']))} {sym}"
            rec.append({**r, 'display': disp})
        nft = None
        if t['kind'] == 'wallet':
            from .nfts import derive
            d = derive(store, t['id'], t['address'])
            if d['derived_from_rows']:
                nft = {'items': d['items'][:10], 'total': len(d['items']), 'rows': d['derived_from_rows'], 'inconsistent': d.get('inconsistent_tokens', 0)}
        out.append({**t, 'chain_name': name, 'sym': sym, 'snapshot': snap, 'scanned_at': ts,
                    'tx_count': store.tx_count(t['id']), 'profile': profile(store.window(t['id']), t['address'], verified=bool((snap or {}).get('verified'))) if t['kind'] == 'contract' else None, 'balance_fmt': fmt(snap['native_balance']) if snap and snap.get('native_balance') else '', 'recent': rec,
                    'nft': nft, 'sanctions_note': (snap or {}).get('sanctions_note'), 'sanctions_date': (snap or {}).get('sanctions_list_date')})
    gas = []
    for ch in sorted({t['chain'] for t in store.targets()}):
        rows = store.gas_rows(ch)
        if rows:
            gas.append({'chain': ch, 'chain_name': CHAINS.get(ch, ('Chain ' + ch, ''))[0], 'latest': rows[0], 'n': len(rows),
                        'min': min(r['propose'] for r in rows), 'max': max(r['propose'] for r in rows),
                        'below': store.get_setting('gas_below_' + ch), 'above': store.get_setting('gas_above_' + ch)})
    return {'targets': out, 'alerts': store.alerts(50), 'gas': gas}


def serve(store_factory, client_factory, host='127.0.0.1', port=8787):
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('dashboard binds to loopback only')
    lock = threading.Lock()
    holder = {}

    def allowed_host(h):
        h = (h or '').rsplit(':', 1)[0] if not (h or '').startswith('[') else (h or '').split(']')[0] + ']'
        return h in ('127.0.0.1', 'localhost', '[::1]')

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, body, ctype, extra=None):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(b)))
            self.send_header('X-Content-Type-Options', 'nosniff'); self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer'); self.send_header('Cache-Control', 'no-store')
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def _host_ok(self):
            if allowed_host(self.headers.get('Host')): return True
            self._send(403, 'bad host', 'text/plain'); return False  # blocks DNS rebinding

        def do_GET(self):
            if not self._host_ok(): return
            if self.path in ('/', '/index.html'):
                n = secrets.token_urlsafe(16)
                csp = (f"default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; "
                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self._send(200, PAGE.replace('__N__', n), 'text/html; charset=utf-8', {'Content-Security-Policy': csp})
            elif self.path == '/api/state':
                self._send(200, json.dumps(state(store_factory()), ensure_ascii=False), 'application/json')
            else:
                self._send(404, 'not found', 'text/plain')

        def do_POST(self):
            if not self._host_ok(): return
            if self.path != '/api/scan':
                return self._send(404, 'not found', 'text/plain')
            origin = self.headers.get('Origin')
            if self.headers.get('X-Dappman') != '1' or (origin and not allowed_host(origin.split('://', 1)[-1])):
                return self._send(403, 'forbidden', 'text/plain')  # CSRF: custom header forces CORS preflight; foreign Origin refused
            if not lock.acquire(blocking=False):
                return self._send(409, '{"error":"scan already running"}', 'application/json')
            try:
                st, cl = store_factory(), client_factory()
                res = scan_all(cl, st)
                self._send(200, json.dumps({'scanned': len(res)}), 'application/json')
            finally:
                lock.release()

    return ThreadingHTTPServer((host, port), H)
