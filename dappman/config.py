"""All tunables in one place: chains, paging, thresholds, public RPC endpoints. No keys, no secrets."""
CHAINS = {'1': ('Ethereum', 'ETH'), '56': ('BNB Smart Chain', 'BNB'), '137': ('Polygon', 'POL'),
          '42161': ('Arbitrum One', 'ETH'), '8453': ('Base', 'ETH'), '10': ('Optimism', 'ETH'),
          '43114': ('Avalanche C-Chain', 'AVAX')}
PAGE = 50            # rows per provider page
MAX_PAGES = 5        # scan pages per category before a coverage gap is flagged
SYNC_MAX_PAGES = 200  # 200 * 50 = the keyless provider's 10,000-record window cap
BIG_NATIVE = '10'    # native-coin alert threshold (units of the chain's coin, not USD)

# Keyless public JSON-RPC endpoints used ONLY for read-only eth_call/eth_blockNumber/eth_chainId (ownership checks).
# Tested reachable on 2026-11-25; public endpoints can rate-limit or disappear, so every result names its host.
RPC_URLS = {
    '1': ['https://ethereum-rpc.publicnode.com'],
    '137': ['https://polygon-bor-rpc.publicnode.com'],
    '8453': ['https://base-rpc.publicnode.com', 'https://mainnet.base.org'],
    '42161': ['https://arbitrum-one-rpc.publicnode.com'],
    '10': ['https://optimism-rpc.publicnode.com'],
    '56': ['https://bsc-rpc.publicnode.com'],
    '43114': ['https://avalanche-c-chain-rpc.publicnode.com'],
}
