"""Selector CONSISTENCY check (not ABI validation, not proof the name is the only function with that selector;
selector collisions are possible) without third-party libs: pure-Python Keccak-256, so a function NAME from the API is only
trusted when keccak(canonical signature)[:4] equals the transaction's real 4-byte selector in its input data."""
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data) + b'\x01'
    p += b'\x00' * ((-len(p)) % rate)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        blk = p[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(blk[8 * i:8 * i + 8], 'little')
        a = _f(a)
    out = b''.join(a[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))
    return out


def canonical_signature(function_name):
    """'approve(address _spender, uint256 _value)' -> 'approve(address,uint256)'. None if tuples or unparseable."""
    if not function_name or '(' not in function_name or not function_name.endswith(')'):
        return None
    name, rest = function_name.split('(', 1)
    inner = rest[:-1]
    if '(' in inner or ')' in inner:
        return None  # tuple parameter: not canonicalised here
    types = [p.strip().split(' ')[0] for p in inner.split(',')] if inner.strip() else []
    return f"{name.strip()}({','.join(types)})"


def selector_matches(function_name, input_data):
    """True only if the labelled function's real selector equals the first 4 bytes of the tx input."""
    sig = canonical_signature(function_name)
    if not sig or not input_data or not input_data.startswith('0x') or len(input_data) < 10:
        return False
    return keccak256(sig.encode())[:4].hex() == input_data[2:10].lower()
