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

# Receipt endpoints for identity verification (eth_getTransactionReceipt). Order matters: the next one is used only when the
# previous gave NO answer (null receipt / network error). Refusals (HTTP 401/403/429) stop the run. Observed 2026-11-25:
# publicnode returned null for many older Ethereum receipts, rpc.mevblocker.io answered all 49 sampled; their terms were not reviewed.
RECEIPT_RPC_URLS = {**RPC_URLS, '1': ['https://ethereum-rpc.publicnode.com', 'https://rpc.mevblocker.io']}

# Trace endpoints (trace_transaction) for internal-transfer identity. Observed 2026-11-26: only eth.drpc.org answered on Ethereum
# among 8 free keyless endpoints tried (others: 401/403/406/method unavailable). Free-tier rate limits and terms were NOT reviewed.
# Refusals (HTTP 401/403/429) stop the run; nothing is rotated around them. Ethereum only: other chains untested.
TRACE_RPC_URLS = {'1': ['https://eth.drpc.org']}
