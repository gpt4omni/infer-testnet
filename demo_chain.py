#!/usr/bin/env python3
"""The coin, not the SQLite: PoW blocks are truth, DB is a rebuildable index.

Proves: genesis -> mined transfers -> full verify -> tamper detected ->
fork/reorg switches truth -> balances rebuilt purely from blocks.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import pq
import chain as C
from chain import apply_tx, rebuild_state
from ledger import connect, register_pq, transfer, slash, log_job, balance_watts
from money import to_watts, fmt_watts

BITS = 2 ** 242  # ~12-bit demo difficulty: ~4k hashes, instant, still real sha256d PoW
TDIR = os.path.join(os.path.dirname(__file__), "chaindemo")


def fresh_db(name):
    p = os.path.join(TDIR, name)
    if os.path.exists(p):
        os.remove(p)
    return connect(p)


def rebuild(blocks, accounts=()):
    return rebuild_state(os.path.join(TDIR, "state.db"), blocks, accounts)


def signed_transfer(c, skey, saddr, to, amount):
    from ledger import next_ots
    pkg = pq.sign(skey, next_ots(c, saddr), saddr, to, amount)
    return {"type": "transfer", "from": saddr, "to": to, "amount": amount, "sig": pkg}


def main():
    os.makedirs(TDIR, exist_ok=True)
    a, b = pq.generate(), pq.generate()
    roots = [(a["addr"], pq.root_of(bytes.fromhex(a["seed"])).hex()),
             (b["addr"], pq.root_of(bytes.fromhex(b["seed"])).hex())]

    # genesis: truth starts here
    g = C.genesis([{"from": "faucet", "to": a["addr"], "amount": to_watts(1000)},
                   {"from": "faucet", "to": b["addr"], "amount": to_watts(1000)}], BITS)
    ch = C.Chain()
    ok, msg = ch.add(g)
    assert ok, msg
    print(f"genesis mined: {C.header_hash(g['header'])[:16]}.. nonce={g['header']['nonce']}")

    # block 1: real PQ-signed transfer, applied to throwaway index
    tmp = fresh_db("tmp.db")
    register_pq(tmp, a["addr"], pq.root_of(bytes.fromhex(a["seed"])).hex())
    register_pq(tmp, b["addr"], pq.root_of(bytes.fromhex(b["seed"])).hex())
    t1 = signed_transfer(tmp, a["seed"], a["addr"], b["addr"], to_watts(100))
    b1 = C.mine_block(C.header_hash(g["header"]), 1, BITS, [t1])
    ok, msg = ch.add(b1)
    assert ok, msg

    state = rebuild(ch.path_to(ch.tip), roots)
    ba, bb = balance_watts(state, a["addr"]), balance_watts(state, b["addr"])
    print(f"after block 1: A={fmt_watts(ba)} B={fmt_watts(bb)}")
    assert (ba, bb) == (to_watts(900), to_watts(1100))

    ok, msg = ch.verify()
    print(f"full chain verify: {msg}")
    assert ok

    # tamper: edit history -> detected
    evil = {"header": dict(b1["header"]), "txs": [dict(t1, amount=to_watts(999999))]}
    ch2 = C.Chain()
    ch2.add(g)
    ok, msg = ch2.add(evil)
    print(f"tampered block: rejected ({msg})")
    assert not ok

    # fork/reorg: heavier competing fork becomes truth
    f1 = C.mine_block(C.header_hash(g["header"]), 1, BITS - 1,
                      [{"type": "slash", "entity": b["addr"], "kind": "miner",
                        "amount": to_watts(10), "reason": "fork demo"}])
    ch.add(f1)
    f2 = C.mine_block(C.header_hash(f1["header"]), 2, BITS - 1, [])
    ch.add(f2)
    print(f"reorg: tip height={ch.height} (fork won on cumulative work)")
    assert ch.height == 2 and ch.tip == C.header_hash(f2["header"])
    state2 = rebuild(ch.path_to(ch.tip), roots)
    print(f"post-reorg B balance: {fmt_watts(balance_watts(state2, b['addr']))} (slash applied, transfer gone)")
    ok, msg = ch.verify()
    assert ok
    print(f"reorged chain verify: {msg}")
    print("CHAIN DEMO PASS: blocks are truth, DB rebuilds from them.")


if __name__ == "__main__":
    main()
