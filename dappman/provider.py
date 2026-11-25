"""Data-provider boundary. monitor/history talk to a Provider, never to tool names or response shapes.
Implemented today: EtherscanMcpProvider (keyless). Another indexer = another subclass, no change to rules/storage."""
import time


class Provider:
    name = 'abstract'

    def balance(self, chain, address): raise NotImplementedError
    def contract_creation(self, chain, address): raise NotImplementedError
    def contract_source(self, chain, address): raise NotImplementedError
    def events(self, kind, chain, address, *, contract=None, page=1, offset=50, sort='desc', startblock=None, endblock=None):
        """Raw provider rows for one category: normal | internal | erc20 | erc721 | erc1155."""
        raise NotImplementedError
    def tip_block(self, chain): raise NotImplementedError


class EtherscanMcpProvider(Provider):
    name = 'etherscan-keyless-mcp'
    _STD = {'erc20', 'erc721', 'erc1155'}

    def __init__(self, client):
        self.client = client

    def balance(self, chain, address):
        return self.client.call('get_native_balance', {'address': address, 'chainid': chain, 'tag': 'latest'})

    def contract_creation(self, chain, address):
        return self.client.call('get_contract_creation', {'contractaddresses': [address], 'chainid': chain})

    def contract_source(self, chain, address):
        return self.client.call('get_contract_source', {'address': address, 'chainid': chain})

    def events(self, kind, chain, address, *, contract=None, page=1, offset=50, sort='desc', startblock=None, endblock=None):
        a = {'chainid': chain, 'page': page, 'offset': offset, 'sort': sort}
        if startblock is not None: a['startblock'] = startblock
        if endblock is not None: a['endblock'] = endblock
        if kind in self._STD:
            tool = 'get_token_transfers'
            a['standard'] = kind
            a['contractaddress' if contract else 'address'] = contract or address
        else:
            tool = {'normal': 'get_transactions', 'internal': 'get_internal_transactions'}[kind]
            a['address'] = address
        return self.client.call(tool, a)

    def tip_block(self, chain):
        from .client import ApiError
        r = self.client.call('get_block_by_timestamp', {'timestamp': int(time.time()), 'closest': 'before', 'chainid': chain})
        b = r.get('blockNumber') if isinstance(r, dict) else r
        if str(b).isdigit():
            return int(b)
        raise ApiError('tip block unavailable: ' + str(r)[:100])


def as_provider(x):
    return x if isinstance(x, Provider) else EtherscanMcpProvider(x)
