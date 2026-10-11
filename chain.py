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
MAX_REORG_DEPTH = 6  # refuse heavier forks branching deeper than this (testnet training wheels)
MTP_WINDOW = 11  # median-time-past window, Bitcoin-style


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
         "timestamp": time.time() if timestamp is None else timestamp, "bits": bits, "nonce": 0}
    target = target_of(bits)
    if type(bits) is not int or not 1 <= target <= MAX_TARGET:
        raise ValueError("target outside allowed range")
    # Only nonce changes. Hash exactly the old canonical JSON, without
    # serializing the other five fields on every attempt.
    prefix, suffix = canon(h).split(b'"nonce":0', 1)
    base = hashlib.sha256(prefix + b'"nonce":')
    target_bytes = target.to_bytes(32, "big")
    while True:
        first = base.copy()
        first.update(str(h["nonce"]).encode() + suffix)
        if hashlib.sha256(first.digest()).digest() <= target_bytes:
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
        self._integer_work = {}  # exact fast path; fractional work keeps legacy summation
        self.tip = ""
        self.height = -1
        self.checkpoint = None  # (height, hash): operator-pinned history, verified externally

    def set_checkpoint(self, height: int, hash_: str):
        self.checkpoint = (height, hash_)

    def _mtp(self, prev_hash: str) -> float:
        ts, b = [], prev_hash
        while b and len(ts) < MTP_WINDOW:
            blk = self.blocks.get(b)
            if blk is None:
                break
            ts.append(blk["header"]["timestamp"])
            b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        ts.sort()
        return ts[len(ts) // 2] if ts else 0.0

    def _ancestor(self, a: str, b_: str) -> str:
        while a and b_ and a != b_:
            ah, bh = self.blocks[a]["header"], self.blocks[b_]["header"]
            if ah["height"] >= bh["height"]:
                a = ah["prev_hash"] if ah["height"] else ""
            if bh["height"] >= ah["height"]:
                b_ = bh["prev_hash"] if bh["height"] else ""
        return a if a == b_ else ""

    def next_bits(self, tip: str) -> int:
        """Same retarget rule, without building the entire chain path."""
        h = self.blocks[tip]["header"]
        if h["height"] < RETARGET or h["height"] % RETARGET:
            return h["bits"]
        tail, b = [], tip
        for _ in range(RETARGET + 1):
            blk = self.blocks[b]
            tail.append(blk)
            b = blk["header"]["prev_hash"]
        return retarget(list(reversed(tail)))

    def add(self, block: dict) -> tuple[bool, str]:
        """Validate + extend. Heaviest chain wins (cumulative work approx =
        sum of target difficulties). Reorgs handled by caller re-applying."""
        if not isinstance(block, dict) or not isinstance(block.get("header"), dict):
            return False, "bad block shape"
        h = block["header"]
        if (any(type(h.get(k)) is not int for k in ("height", "bits", "nonce")) or
                h["height"] < 0 or h["nonce"] < 0 or not 1 <= h["bits"] <= MAX_TARGET):
            return False, "bad header integers"
        import math
        if (type(h.get("timestamp")) not in (int, float) or
                not math.isfinite(h["timestamp"]) or h["timestamp"] < 0):
            return False, "bad timestamp"
        for k in ("prev_hash", "merkle_root"):
            v = h.get(k)
            if not isinstance(v, str) or len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
                return False, "bad header hash"
        if not isinstance(block.get("txs"), list) or any(
                not isinstance(t, dict) or not isinstance(t.get("type"), str) for t in block["txs"]):
            return False, "bad transactions"
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
            if h["timestamp"] <= self._mtp(h["prev_hash"]):
                return False, "timestamp not above median-past"
        bh = header_hash(h)
        try:
            money_ok = self._valid_money(block)
        except (KeyError, TypeError, ValueError):
            money_ok = False
        if not money_ok:
            return False, "bad coinbase (only one capped subsidy mint, first tx only)"
        if bh in self.blocks:
            return True, "already known"
        self.blocks[bh] = block
        unit = MAX_TARGET / h["bits"]
        parent_work = self._integer_work.get(h["prev_hash"], 0 if h["height"] == 0 else None)
        if parent_work is not None and unit.is_integer() and parent_work + int(unit) <= 2 ** 53:
            self._integer_work[bh] = parent_work + int(unit)
        if self.work(bh) > self.work(self.tip):
            if (self.checkpoint and h["height"] >= self.checkpoint[0] and
                    not (h["prev_hash"] == self.tip and self.height >= self.checkpoint[0])):
                b = bh
                while self.blocks[b]["header"]["height"] > self.checkpoint[0]:
                    b = self.blocks[b]["header"]["prev_hash"]
                if b != self.checkpoint[1]:
                    return True, "stored, checkpoint mismatch refused"
            if self.tip and h["prev_hash"] != self.tip:
                anc = self._ancestor(bh, self.tip)
                fork_h = self.blocks[anc]["header"]["height"] if anc else -1
                if self.checkpoint and fork_h < self.checkpoint[0]:
                    return True, "stored, reorg below checkpoint refused"
                if self.height - fork_h > MAX_REORG_DEPTH:
                    return True, f"stored, reorg too deep (> {MAX_REORG_DEPTH})"
            self.tip = bh
            self.height = h["height"]
        return True, "ok"

    def _valid_money(self, block: dict) -> bool:
        txs = block["txs"]
        if any(type(t.get("amount")) is not int or t["amount"] <= 0 for t in txs if t["type"] == "mint"):
            return False
        if block["header"]["height"] == 0:
            return all(t["type"] == "mint" and t.get("from", "faucet") == "faucet" for t in txs)
        subs = [t for t in txs if t["type"] == "mint" and t.get("from") == "subsidy"]
        if sum(t["amount"] for t in subs) > MAX_BLOCK_REWARD_WATTS:
            return False  # total subsidy per block capped — the split lives inside it
        if not subs:
            return True  # no coinbase claimed; allowed (feeless block)
        if txs[0] is not subs[0]:
            return False  # producer coinbase must lead
        job_miners = {t["miner"] for t in txs if t["type"] == "job"}
        for s in subs[1:]:
            # every extra subsidy mint must reward a job-miner IN this block
            if s["to"] not in job_miners:
                return False
        return True

    def work(self, tip: str) -> float:
        if tip in self._integer_work:
            return float(self._integer_work[tip])
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
        path, check = self.path_to(self.tip), Chain()
        for blk in path:
            h = blk["header"]
            ok, msg = check.add(blk)
            if not ok:
                return False, f"{msg} at {h['height']}"
        if self.checkpoint:
            ch, chh = self.checkpoint
            hit = [b for b in path if b["header"]["height"] == ch]
            if not hit or header_hash(hit[0]["header"]) != chh:
                return False, "checkpoint not on tip path"
        return True, f"ok ({len(path)} blocks)"


def split_coinbase(total: int, producer_addr: str, job_txs: list[dict],
                   producer_share: float = 0.5, height: int | None = None) -> list[dict]:
    """Split a block's subsidy: producer takes producer_share, the rest goes
    pro-rata by job pay to miners with jobs in THIS block (dust < 1 watt
    skipped, remainder to producer so the sum is exact). `height` stamps
    each mint so identical rewards in different blocks stay distinct txids
    (idempotent apply would otherwise eat repeats)."""
    prod = int(total * producer_share)
    mints = []
    tag = {"h": height} if height is not None else {}
    if prod > 0:
        mints.append({"type": "mint", "from": "subsidy", "to": producer_addr, "amount": prod, **tag})
    pool = total - prod
    by_miner: dict[str, int] = {}
    for t in job_txs:
        if t.get("type") == "job" and t.get("pay", 0) > 0:
            by_miner[t["miner"]] = by_miner.get(t["miner"], 0) + t["pay"]
    tot = sum(by_miner.values())
    given = 0
    for m, sc in sorted(by_miner.items(), key=lambda kv: -kv[1]):
        amt = int(pool * sc / tot) if tot else 0
        if amt >= 1:
            mints.append({"type": "mint", "from": "subsidy", "to": m, "amount": amt, **tag})
            given += amt
    if mints:
        mints[0]["amount"] += total - prod - given  # exact sum, dust to producer
    return mints


def genesis(mints: list[dict], bits: int) -> dict:
    return mine_block("00" * 32, 0, bits, [{"type": "mint", **m} for m in mints])


def block_record(block: dict) -> bytes:
    return (json.dumps(block, sort_keys=True) + "\n").encode()


def save_block(f, block: dict):
    f.write(block_record(block))


def load_chain(path: str, recover_tail=False) -> "Chain":
    """Validate every record. Only an interrupted final write can be repaired."""
    ch = Chain()
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return ch
    repair, line_no = False, 0
    with fh:
        while True:
            offset, line = fh.tell(), fh.readline()
            if not line:
                break
            line_no += 1
            terminated = line.endswith(b"\n")
            if not line.strip():
                continue
            try:
                ok, msg = ch.add(json.loads(line))
            except json.JSONDecodeError as e:
                if recover_tail and not terminated and ch.tip:
                    repair = offset
                    break
                raise ValueError(f"chain.dat corrupt at record {line_no}, byte {offset}: {e}") from e
            except (ValueError, TypeError, KeyError) as e:
                raise ValueError(f"chain.dat corrupt at record {line_no}, byte {offset}: {e}") from e
            if not ok:
                raise ValueError(f"chain.dat corrupt at record {line_no}, byte {offset}: {msg}")
            if not terminated and recover_tail:
                repair = None
    if repair is not False:
        from storage import repair_tail
        backup = repair_tail(path, repair)
        print(f"recovered interrupted chain tail: height={ch.height}; original saved at {backup}", flush=True)
    return ch


# ---------------------------------------------------------------------------
# Chain -> ledger application. SQLite is a disposable index: drop it and
# rebuild_state() reproduces every balance from blocks alone.
# ---------------------------------------------------------------------------

def apply_tx(c, tx: dict):
    """Commit the state change and replay marker in one SQLite transaction."""
    with c:
        return _apply_tx(c, tx)


def _apply_tx(c, tx: dict):
    """Apply one tx idempotently: already-seen txids (re-mined across forks
    or duplicate mempool inclusion) are skipped, so replays never double-pay."""
    from ledger import transfer, slash, log_job
    c.execute("CREATE TABLE IF NOT EXISTS applied_txid(txid TEXT PRIMARY KEY)")
    tid = txid(tx)
    if c.execute("SELECT 1 FROM applied_txid WHERE txid=?", (tid,)).fetchone():
        return "dup"
    t = tx["type"]
    if t == "mint":
        transfer(c, tx.get("from", "faucet"), tx["to"], tx["amount"], commit=False)
        c.execute("CREATE TABLE IF NOT EXISTS chain_mints(txid TEXT PRIMARY KEY, transfer_id INTEGER)")
        c.execute("INSERT INTO chain_mints VALUES(?,last_insert_rowid())", (tid,))
    elif t == "transfer":
        transfer(c, tx["from"], tx["to"], tx["amount"], tx["sig"], commit=False, chain_txid=tid)
    elif t == "slash":
        slash(c, tx["entity"], tx["kind"], tx["amount"], tx["reason"], commit=False)
        c.execute("INSERT INTO chain_slashes VALUES(?,?,?,?)", (tid, tx["entity"], tx["kind"], tx["amount"]))
    elif t == "job":
        log_job(c, tx["miner"], tx["ph"], tx["oh"], tx["tokens"], tx["lat"],
                tx["eval"], tx["passed"], tx["score"], tx["pay"], commit=False)
    else:
        raise ValueError(f"unknown tx type {t}")
    c.execute("INSERT INTO applied_txid VALUES(?)", (tid,))
    return "ok"


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
