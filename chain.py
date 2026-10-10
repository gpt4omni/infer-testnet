#!/usr/bin/env python3
"""Infer chain: real PoW blocks, the thing SQLite wasn't.

Design (Nakamoto-lite, testnet):
- Block = header {height, prev_hash, merkle_root, timestamp, bits, nonce}
  + ordered txs. Header hash = sha256d(canonical header). Valid iff hash
  <= target(bits). Chain = heaviest valid tip wins; reorgs revert + reapply.
- Txs are canonical JSON dicts. Types: mint (protocol subsidy/faucet),
  transfer (PQ-signed), htlc_settle (protocol execution of authorized
  HTLC), job (score receipt, no money movement), slash (protocol).
- Difficulty retargets every RETARGET blocks toward BLOCK_TIME_SEC using
  clampled actual timespan — same feedback as Bitcoin, toy constants.
- SQLite stays, but ONLY as a chainstate index (like Bitcoin Core's
  LevelDB): it may be rebuilt from blocks at any time. Blocks are truth.

Quantum note: block hashes are SHA-256d (Grover only); block *authority*
comes from work, transfers from PQ sigs. Shor has nothing to chew on.
"""
import hashlib
import json
import time

HASH = lambda b: hashlib.sha256(hashlib.sha256(b).digest()).digest()
MAX_TARGET = 2 ** 248  # easiest allowed (testnet; mainnet would be far lower)
RETARGET = 10
BLOCK_TIME_SEC = 30
CLAMP = 4  # max 4x swing per retarget, like Bitcoin
# Testnet block-reward cap (mainnet: money.daily_subsidy_infr prorated).
# Enforced in add(): exactly one subsidy mint allowed, first tx only.
MAX_BLOCK_REWARD_WATTS = 5_000_000  # 5 INFR


def canon(o) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()


def txid(tx: dict) -> str:
    return HASH(canon(tx)).hex()


def merkle_root(txids: list[str]) -> str:
    if not txids:
        return "00" * 32
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [HASH(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


def target_of(bits: int) -> int:
    return bits  # bits stores the integer target directly (testnet-simple)


def header_hash(h: dict) -> str:
    return HASH(canon({k: h[k] for k in ("height", "prev_hash", "merkle_root", "timestamp", "bits", "nonce")})).hex()


def mine_block(prev_hash: str, height: int, bits: int, txs: list[dict], timestamp=None) -> dict:
    root = merkle_root([txid(t) for t in txs])
    h = {"height": height, "prev_hash": prev_hash, "merkle_root": root,
         "timestamp": timestamp or time.time(), "bits": bits, "nonce": 0}
    target = target_of(bits)
    while True:
        if int(header_hash(h), 16) <= target:
            return {"header": h, "txs": txs}
        h["nonce"] += 1


def valid_pow(block: dict) -> bool:
    h = block["header"]
    return int(header_hash(h), 16) <= target_of(h["bits"]) and h["bits"] <= MAX_TARGET


def retarget(chain) -> int:
    """Next difficulty from last RETARGET blocks' actual timespan."""
    if len(chain) < RETARGET + 1 or (len(chain) - 1) % RETARGET:
        return chain[-1]["header"]["bits"]
    first = chain[-RETARGET - 1]["header"]["timestamp"]
    last = chain[-1]["header"]["timestamp"]
    actual = max(1.0, min(last - first, RETARGET * BLOCK_TIME_SEC * CLAMP))
    actual = max(RETARGET * BLOCK_TIME_SEC / CLAMP, actual)
    prev = target_of(chain[-1]["header"]["bits"])
    nxt = min(MAX_TARGET, int(prev * actual / (RETARGET * BLOCK_TIME_SEC)))
    return max(1, nxt)


class Chain:
    def __init__(self):
        self.blocks: dict[str, dict] = {}  # hash -> block
        self.tip = ""
        self.height = -1

    def add(self, block: dict) -> tuple[bool, str]:
        """Validate + extend. Heaviest chain wins (cumulative work approx =
        sum of target difficulties). Reorgs handled by caller re-applying."""
        h = block["header"]
        if not valid_pow(block):
            return False, "bad proof-of-work"
        if h["merkle_root"] != merkle_root([txid(t) for t in block["txs"]]):
            return False, "bad merkle root"
        if h["height"] == 0:
            if h["prev_hash"] != "00" * 32:
                return False, "bad genesis prev"
        else:
            prev = self.blocks.get(h["prev_hash"])
            if prev is None:
                return False, "prev unknown (orphan)"
            if h["height"] != prev["header"]["height"] + 1:
                return False, "bad height"
            if h["timestamp"] < prev["header"]["timestamp"]:
                return False, "timestamp regression"
        bh = header_hash(h)
        if not self._valid_money(block):
            return False, "bad coinbase (only one capped subsidy mint, first tx only)"
        self.blocks[bh] = block
        if self.work(bh) > self.work(self.tip):
            self.tip = bh
            self.height = h["height"]
        return True, "ok"

    def _valid_money(self, block: dict) -> bool:
        txs = block["txs"]
        if block["header"]["height"] == 0:
            return all(t["type"] == "mint" and t.get("from", "faucet") == "faucet" for t in txs)
        subs = [t for t in txs if t["type"] == "mint" and t.get("from") == "subsidy"]
        if len(subs) > 1:
            return False
        if subs and (txs[0] is not subs[0] or subs[0]["amount"] > MAX_BLOCK_REWARD_WATTS):
            return False
        return True

    def work(self, tip: str) -> float:
        w, b = 0.0, tip
        while b:
            blk = self.blocks.get(b)
            if blk is None:
                break
            w += MAX_TARGET / max(1, target_of(blk["header"]["bits"]))
            b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        return w

    def path_to(self, tip: str) -> list[dict]:
        out, b = [], tip
        while b:
            blk = self.blocks[b]
            out.append(blk)
            b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        return list(reversed(out))

    def verify(self) -> tuple[bool, str]:
        """Full revalidation from genesis along current tip."""
        if not self.tip:
            return True, "empty"
        for blk in self.path_to(self.tip):
            h = blk["header"]
            if not valid_pow(blk):
                return False, f"bad PoW at {h['height']}"
            if h["merkle_root"] != merkle_root([txid(t) for t in blk["txs"]]):
                return False, f"bad merkle at {h['height']}"
        # link check
        path = self.path_to(self.tip)
        for a, b in zip(path, path[1:]):
            if b["header"]["prev_hash"] != header_hash(a["header"]):
                return False, f"broken link at {b['header']['height']}"
        return True, f"ok ({len(path)} blocks)"


def genesis(mints: list[dict], bits: int) -> dict:
    return mine_block("00" * 32, 0, bits, [{"type": "mint", **m} for m in mints])


def save_block(f, block: dict):
    f.write((json.dumps(block, sort_keys=True) + "\n").encode())


def load_chain(path: str) -> "Chain":
    """Load + fully validate every block. Corrupt tail stops load (fail-closed)."""
    ch = Chain()
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return ch
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ok, msg = ch.add(json.loads(line))
            if not ok:
                raise ValueError(f"chain.dat corrupt: {msg}")
    return ch


# ---------------------------------------------------------------------------
# Chain -> ledger application. SQLite is a disposable index: drop it and
# rebuild_state() reproduces every balance from blocks alone.
# ---------------------------------------------------------------------------

def apply_tx(c, tx: dict):
    from ledger import transfer, slash, log_job
    t = tx["type"]
    if t == "mint":
        transfer(c, tx.get("from", "faucet"), tx["to"], tx["amount"])
    elif t == "transfer":
        transfer(c, tx["from"], tx["to"], tx["amount"], tx["sig"])
    elif t == "slash":
        slash(c, tx["entity"], tx["kind"], tx["amount"], tx["reason"])
    elif t == "job":
        log_job(c, tx["miner"], tx["ph"], tx["oh"], tx["tokens"], tx["lat"],
                tx["eval"], tx["passed"], tx["score"], tx["pay"])
    else:
        raise ValueError(f"unknown tx type {t}")


def rebuild_state(db_path: str, blocks: list[dict], accounts=()):
    """Wipe + replay blocks into a fresh index. Returns connection."""
    import os as _os
    from ledger import connect, register_pq
    if _os.path.exists(db_path):
        _os.remove(db_path)
    c = connect(db_path)
    for addr, root in accounts:
        register_pq(c, addr, root)
    for b in blocks:
        for t in b["txs"]:
            apply_tx(c, t)
    return c
