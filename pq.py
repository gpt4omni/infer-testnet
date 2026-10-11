#!/usr/bin/env python3
"""Post-quantum wallet signatures (testnet-grade XMSS-lite).

Why not ECDSA/secp256k1: Shor's algorithm breaks discrete logs, so every
ECDSA key is quantum-vulnerable. Hash-based signatures reduce security to
hash preimage resistance only — Grover's algorithm merely halves it, so
SHA-256 still gives ~128-bit post-quantum strength. No lattice trusted
setup, no new hardness assumptions, pure hashlib stdlib.

Scheme: WOTS-SHA256 (w=16, len=67) leaves under a Merkle tree of height 8
(= 256 one-time keys per wallet). Simplified vs RFC 8391: no bitmask
randomization in chains (noted deviation; fine for testnet, revisit for
mainnet alongside full WOTS+ and 2x hash output).

Rules enforced by ledger.transfer: one OTS index per transfer, index
never reused (replay/reuse rejected), message binds parties+amount+index.
"""
import hashlib
import secrets

N = 32            # SHA-256 output
W = 16            # Winternitz parameter
LEN1 = 64         # 256 bits / 4 bits per digit
LEN2 = 3          # checksum digits (max csum 64*15=960 < 16^3)
LEN = LEN1 + LEN2
HEIGHT = 8
NKEYS = 2 ** HEIGHT


def _h(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


def _chain(x: bytes, steps: int) -> bytes:
    for _ in range(steps):
        x = _h(x)
    return x


def _base16_nibbles(data: bytes) -> list[int]:
    out = []
    for b in data:
        out += [(b >> 4) & 15, b & 15]
    return out


def _wots_sk(seed: bytes, idx: int) -> list[bytes]:
    return [_h(b"wots", seed, bytes([idx]), bytes([j])) for j in range(LEN)]


def _wots_pk(sk: list[bytes]) -> list[bytes]:
    return [_chain(s, W - 1) for s in sk]


def _wots_pk_hash(pk: list[bytes]) -> bytes:
    return _h(b"wotspk", b"".join(pk))


def _digits(msg32: bytes) -> list[int]:
    d = _base16_nibbles(msg32)
    csum = sum((W - 1) - x for x in d)
    cd = _base16_nibbles(csum.to_bytes(2, "big"))[-LEN2:]
    return d + cd


def _wots_sign(sk: list[bytes], msg32: bytes) -> list[bytes]:
    return [_chain(s, st) for s, st in zip(sk, _digits(msg32))]


def _wots_verify(sig: list[bytes], msg32: bytes, pk: list[bytes]) -> bool:
    if len(sig) != LEN or len(pk) != LEN:
        return False
    return all(_chain(s, (W - 1) - st) == p for s, st, p in zip(sig, _digits(msg32), pk))


def _leaf(seed: bytes, idx: int) -> bytes:
    return _h(b"leaf", _wots_pk_hash(_wots_pk(_wots_sk(seed, idx))))


def _root_and_path(seed: bytes, idx: int) -> tuple[bytes, list[bytes]]:
    level = [_leaf(seed, i) for i in range(NKEYS)]
    path, j = [], idx
    while len(level) > 1:
        path.append(level[j ^ 1])
        nxt = []
        for k in range(0, len(level), 2):
            nxt.append(_h(b"node", level[k], level[k + 1]))
        level, j = nxt, j // 2
    return level[0], path


def root_of(seed: bytes) -> bytes:
    return _root_and_path(seed, 0)[0]


def address_of(root: bytes) -> str:
    return "pq1" + _h(b"addr", root).hex()[:32]


def generate() -> dict:
    seed = secrets.token_bytes(N)
    return {"seed": seed.hex(), "addr": address_of(root_of(seed))}


def _msg(from_addr: str, to_addr: str, amount: int, ots: int) -> bytes:
    return hashlib.sha256(f"{from_addr}|{to_addr}|{amount}|{ots}".encode()).digest()


def sign(seed_hex: str, ots: int, from_addr: str, to_addr: str, amount: int) -> dict:
    if not 0 <= ots < NKEYS:
        raise ValueError("OTS index exhausted — rotate wallet key")
    seed = bytes.fromhex(seed_hex)
    sk = _wots_sk(seed, ots)
    pk = _wots_pk(sk)
    return {"ots": ots,
            "sig": [s.hex() for s in _wots_sign(sk, _msg(from_addr, to_addr, amount, ots))],
            "path": [p.hex() for p in _root_and_path(seed, ots)[1]],
            "pk": [p.hex() for p in pk]}


def signature_root(pkg: dict) -> str:
    """Recover the public Merkle root; callers must still verify the signature."""
    path = [bytes.fromhex(s) for s in pkg["path"]]
    pk = [bytes.fromhex(s) for s in pkg["pk"]]
    ots = int(pkg["ots"])
    if (not 0 <= ots < NKEYS or len(path) != HEIGHT or len(pk) != LEN or
            any(len(p) != N for p in path + pk)):
        raise ValueError("bad WOTS public key or Merkle path")
    node, j = _h(b"leaf", _wots_pk_hash(pk)), ots
    for sib in path:
        node = _h(b"node", sib, node) if j & 1 else _h(b"node", node, sib)
        j //= 2
    return node.hex()


def verify(from_addr: str, root_hex: str, pkg: dict, to_addr: str, amount: int) -> bool:
    try:
        sig = [bytes.fromhex(s) for s in pkg["sig"]]
        pk = [bytes.fromhex(s) for s in pkg["pk"]]
        ots = int(pkg["ots"])
        if any(len(s) != N for s in sig):
            return False
        if not _wots_verify(sig, _msg(from_addr, to_addr, amount, ots), pk):
            return False
        return signature_root(pkg) == root_hex and address_of(bytes.fromhex(root_hex)) == from_addr
    except (ValueError, KeyError, TypeError):
        return False
