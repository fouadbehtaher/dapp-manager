#!/usr/bin/env python3
"""dApp contract manager - first slice (read-only, keyless Etherscan MCP).
Usage: python3 dapp_monitor.py <outdir> <chainid:0xcontract> [chainid:0xcontract ...]
Builds an Arabic DOCX 'management panel' per contract: identity, verification,
creation, recent activity pulse. No keys, no signing, no writes."""
import csv, json, os, sys, time, urllib.request, uuid
from decimal import Decimal

MCP = 'https://mcp-keyless.etherscan.io/mcp'
SID = None
CHAINS = {'1': ('Ethereum', 'ETH'), '137': ('Polygon', 'POL'), '42161': ('Arbitrum One', 'ETH')}

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
        if SID is None: SID = r.headers.get('mcp-session-id')
        raw = r.read().decode()
    for line in raw.splitlines():
        if line.startswith('data: '): return json.loads(line[6:])
    return {}

def init():
    rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'dapp-manager-slice', 'version': '0.1'}}, 1)
    rpc('notifications/initialized')

def call(tool, args):
    try:
        d = rpc('tools/call', {'name': tool, 'arguments': args}, uuid.uuid4().int % 10**9)
        p = json.loads(d['result']['content'][0]['text'])
    except Exception as e:
        return ('error', str(e)[:120])
    if p.get('ok') and p.get('status') == '1': return ('ok', p.get('result'))
    m = p.get('message') or ''
    if isinstance(p.get('result'), str) and 'No ' in str(p.get('result')): return ('ok', [])
    return ('error', str(p.get('result') or m)[:120])

def ts(t):
    try: return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(int(t)))
    except Exception: return '?'

def main():
    outdir = sys.argv[1]
    targets = [a.split(':', 1) for a in sys.argv[2:]]
    os.makedirs(outdir, exist_ok=True)
    init()
    sections = []
    for cid, ca in targets:
        cname, sym = CHAINS.get(cid, (f'Chain {cid}', ''))
        s = {'chain': cname, 'addr': ca, 'errors': []}
        st, r = call('get_contract_creation', {'contractaddresses': [ca], 'chainid': cid})
        if st == 'ok' and r:
            s['creator'] = r[0].get('contractCreator')
            s['created'] = ts(r[0].get('timestamp'))
            s['tx'] = r[0].get('txHash')
        else:
            s['errors'].append(f'creation: {r}')
        st, r = call('get_contract_source', {'address': ca, 'chainid': cid})
        if st == 'ok' and r:
            s['name'] = r[0].get('ContractName') or '?'
            s['verified'] = bool(r[0].get('SourceCode'))
        else:
            s['errors'].append(f'source: {r}')
        st, r = call('get_transactions', {'address': ca, 'chainid': cid, 'page': 1, 'offset': 25, 'sort': 'desc'})
        if st == 'ok':
            s['recent_tx'] = len(r)
            s['latest_tx'] = ts(r[0]['timeStamp']) if r else 'لا يوجد'
            s['failed_recent'] = sum(1 for t in r if t.get('isError') == '1')
        else:
            s['errors'].append(f'tx: {r}')
        st, r = call('get_token_transfers', {'contractaddress': ca, 'chainid': cid, 'standard': 'erc20', 'page': 1, 'offset': 25, 'sort': 'desc'})
        if st == 'ok':
            s['recent_tt'] = len(r)
            s['latest_tt'] = ts(r[0]['timeStamp']) if r else 'لا يوجد'
        sections.append(s)
    with open(os.path.join(outdir, 'monitor_summary.json'), 'w') as f:
        json.dump(sections, f, ensure_ascii=False, indent=1)
    print(json.dumps(sections, ensure_ascii=False))

if __name__ == '__main__':
    main()
