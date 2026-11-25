"""LIVE acceptance (network, keyless Etherscan MCP). Not part of pytest. Usage: python tests/live_acceptance.py [chainid ...]
For each chain: finds a recent multi-event NFT tx via the provider, syncs the receiving wallet over a bounded recent
block range, then compares the provider's raw row count with the stored row count (and re-syncs to prove idempotency)."""
import sys, collections
sys.path.insert(0, '.')
from dappman.client import McpClient
from dappman.store import Store
from dappman.history import sync_target, tip_block
from dappman.monitor import _tx

BIG = {'1': '0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d',  # BAYC (Ethereum)
       '137': '0xdb46d1dc155634fbc732f92e853b10b288ad5a1d',  # Lens Profile NFT (Polygon)
       '8453': '0x03c4738ee98ae44591e1a4a4f3cab6641d95dd9a'}  # Basenames registrar (Base)

def run(chain, contract):
    c = McpClient(); s = Store(':memory:')
    tip = tip_block(c, chain); lo = tip - 20000
    raw = c.call('get_token_transfers', {'contractaddress': contract, 'standard': 'erc721', 'chainid': chain, 'startblock': lo, 'endblock': tip, 'page': 1, 'offset': 100, 'sort': 'desc'})
    grp = collections.Counter((r['hash'], r['to']) for r in raw)
    (h, wallet), n = max(grp.items(), key=lambda x: x[1])
    t = s.add_target('wallet', chain, wallet.lower(), 'acc'); s.set_setting(f'sync_{t}_erc721', lo)
    tgt = [x for x in s.targets() if x['id'] == t][0]
    rep = sync_target(c, s, tgt, _tx, kinds=('erc721',), window=10000)
    pr = c.call('get_token_transfers', {'address': wallet, 'standard': 'erc721', 'chainid': chain, 'startblock': lo, 'endblock': tip, 'page': 1, 'offset': 100, 'sort': 'asc'})
    got = s.tx_count(t); first = got
    rep2 = sync_target(c, s, tgt, _tx, kinds=('erc721',), window=10000)
    s.set_setting(f'sync_{t}_erc721', lo); rep3 = sync_target(c, s, tgt, _tx, kinds=('erc721',), window=10000)
    print(f'chain {chain}: wallet {wallet[:10]}.. provider_rows={len(pr)} stored={first} (batch tx had {n} events) after_full_resync={s.tx_count(t)} ambiguous={s.ambiguous_rows(t)} status={rep["categories"]["erc721"]["status"]}')
    nego = None
    ok = len(pr) == first == s.tx_count(t)
    amb = s.ambiguous_rows(t)
    print(('  COUNT MATCH' if ok else '  MISMATCH (provider page cap 100 or window edge: inspect)')
          + ('; completeness UNPROVEN: provider gives no log index' + (f', {amb} rows have identical twins' if amb else '')))
    return ok

if __name__ == '__main__':
    chains = sys.argv[1:] or ['1']
    for ch in chains:
        try: run(ch, BIG.get(ch) or sys.argv[0])
        except Exception as e: print('chain', ch, 'ERROR', str(e)[:200])
