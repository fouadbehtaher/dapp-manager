"""Etherscan keyless MCP client. Read-only: only calls data tools, never signs or sends."""
import json, time, urllib.request, uuid

MCP_URL = 'https://mcp-keyless.etherscan.io/mcp'
READ_TOOLS = {'get_native_balance', 'get_transactions', 'get_internal_transactions',
              'get_token_transfers', 'get_contract_source', 'get_contract_creation',
              'get_transaction_status', 'get_supported_chains', 'get_transaction_receipt_status',
              'get_transaction_by_hash', 'get_gas_oracle'}
PROXY_TOOLS = {'get_transaction_by_hash'}  # payload is {ok, result} with no status field; result None = unknown hash


class ApiError(Exception):
    pass


class McpClient:
    def __init__(self, url=MCP_URL, sleep=0.25, retries=3, timeout=40):
        self.url, self.sleep, self.retries, self.timeout = url, sleep, retries, timeout
        self.sid = None
        self.calls = 0

    def _rpc(self, method, params=None, rid=None):
        body = {'jsonrpc': '2.0', 'method': method}
        if rid is not None: body['id'] = rid
        if params is not None: body['params'] = params
        h = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if self.sid: h['Mcp-Session-Id'] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if self.sid is None: self.sid = r.headers.get('mcp-session-id')
            raw = r.read().decode()
        for line in raw.splitlines():
            if line.startswith('data: '): return json.loads(line[6:])
        return {}

    def _init(self):
        if self.sid: return
        self._rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                 'clientInfo': {'name': 'dappman', 'version': '0.3'}}, 1)
        self._rpc('notifications/initialized')

    def call(self, tool, args):
        """Return the result list/str. Raise ApiError on failure (never returns fake zeros)."""
        if tool not in READ_TOOLS:
            raise ApiError('tool not allowed (read-only): ' + tool)
        last = None
        for i in range(self.retries):
            try:
                self._init()
                self.calls += 1
                d = self._rpc('tools/call', {'name': tool, 'arguments': args}, uuid.uuid4().int % 10**9)
                p = json.loads(d['result']['content'][0]['text'])
            except Exception as e:
                last = str(e)[:150]; self.sid = None; time.sleep(1 + i); continue
            time.sleep(self.sleep)
            if tool in PROXY_TOOLS and p.get('ok') is True and 'result' in p:
                return p.get('result')
            if p.get('ok') and p.get('status') == '1':
                return p.get('result')
            res = p.get('result')
            if isinstance(res, str) and 'No ' in res and 'found' in res:
                return []
            msg = str(res or p.get('message') or 'unknown')[:150]
            if 'rate' in msg.lower():
                last = msg; time.sleep(2 + 2 * i); continue
            raise ApiError(msg)
        raise ApiError(last or 'failed')
