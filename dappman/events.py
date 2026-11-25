"""One place that turns a provider row into a stored event and decides what identifies an event."""
from .abi import selector_matches


def _method(r):
    """A function NAME is kept only if keccak(signature)[:4] equals the tx input's real selector; otherwise the raw
    method id is kept, so an untrusted/mismatched label can never trigger a sensitive-call alert."""
    fn, mid, inp = r.get('functionName') or '', r.get('methodId') or '', r.get('input') or ''
    if fn and selector_matches(fn, inp):
        return fn.split('(')[0]
    return mid if mid and mid != '0x' else ''


def to_event(r, kind):
    """kind: normal | internal | erc20 | erc721 | erc1155. internal rows carry no function name, so they can never
    raise a sensitive_call alert. NFT rows keep contract/token_id in their own columns."""
    nft = kind in ('erc721', 'erc1155')
    return {'hash': r.get('hash') or r.get('transactionHash', ''), 'block': int(r.get('blockNumber') or 0),
            'ts': int(r.get('timeStamp') or 0), 'frm': (r.get('from') or '').lower(), 'to': (r.get('to') or '').lower(),
            'value': str(r.get('tokenValue') if nft and kind == 'erc1155' else ('1' if nft else r.get('value') or '0')),
            'failed': 1 if r.get('isError') == '1' else 0,
            'method': _method(r) if not nft and kind != 'internal' else '', 'kind': kind,
            'token': (r.get('tokenSymbol') or '')[:20] + ('|' + str(r.get('tokenDecimal') or '') if kind == 'erc20' else ''),
            'contract': (r.get('contractAddress') or '').lower() if (nft or kind == 'erc20') else '',
            'trace': (r.get('traceId') or '') if kind == 'internal' else '',
            'token_id': str(r.get('tokenID') or '') if nft else ''}


def event_ident(r):
    """Identity of one on-chain event inside a transaction, from what the provider actually returns.
    internal: the provider's traceId. token transfers: contract + token id + from + to + amount. The provider returns
    NO log index for token transfers, so two byte-identical events in one tx are indistinguishable from a provider
    duplicate; they get an ordinal by arrival order and are flagged dup_group>1 (identity ambiguous, never claimed unique)."""
    k = r['kind']
    if k == 'normal':
        return ''
    if k == 'internal':
        return 'trace:' + str(r['trace']) if r.get('trace') not in (None, '') else '%s|%s|%s' % (r['frm'], r['to'], r['value'])
    return '|'.join((r.get('contract', ''), str(r.get('token_id', '')), r['frm'], r['to'], str(r['value'])))
