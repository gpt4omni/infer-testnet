#!/usr/bin/env python3
"""Block gossip: node A mines, node B syncs over TCP and converges.
Every block revalidated on receipt (PoW + merkle + links)."""
import os
import socket
import sys
import threading
sys.path.insert(0, os.path.dirname(__file__))

import chain as C
from ledger import connect
from money import to_watts
from net import GatewayServer, send_msg, recv_msg

DBA = os.path.join(os.path.dirname(__file__), "ledger-gossip-a.db")
DBB = os.path.join(os.path.dirname(__file__), "ledger-gossip-b.db")
BITS = 2 ** 242


def rpc(port, obj):
    s = socket.create_connection(("127.0.0.1", port), timeout=15)
    with s, s.makefile("rwb") as f:
        send_msg(f, obj)
        return recv_msg(f)


def main():
    for p in (DBA, DBB, "/tmp/opencode/gossip-a.dat", "/tmp/opencode/gossip-b.dat"):
        if os.path.exists(p):
            os.remove(p)
    a = GatewayServer(connect(DBA, check_same_thread=False), port=19195,
                        chain_path="/tmp/opencode/gossip-a.dat")
    b = GatewayServer(connect(DBB, check_same_thread=False), port=19196,
                        chain_path="/tmp/opencode/gossip-b.dat")
    threading.Thread(target=a.serve_forever, daemon=True).start()
    threading.Thread(target=b.serve_forever, daemon=True).start()

    # A mines 3 blocks (mints only — no sigs needed for chain validation)
    g = C.genesis([{"from": "faucet", "to": "pq1alice", "amount": to_watts(100)}], BITS)
    assert a.chain.add(g)[0]
    tip = C.header_hash(g["header"])
    for h in (1, 2, 3):
        blk = C.mine_block(tip, h, BITS, [{"type": "mint", "from": "subsidy",
                                           "to": "pq1alice", "amount": to_watts(1)}])
        assert a.chain.add(blk)[0]
        tip = C.header_hash(blk["header"])
    print(f"A tip: height={a.chain.height} {a.chain.tip[:16]}..")

    # B syncs: walk A's chain from tip back to genesis, replay forward
    hashes = []
    h = rpc(19195, {"op": "get_tip"})["tip"]
    while True:
        r = rpc(19195, {"op": "get_block", "hash": h})
        assert r.get("ok"), r
        hashes.append(h)
        if r["block"]["header"]["height"] == 0:
            break
        h = r["block"]["header"]["prev_hash"]
    print(f"fetched {len(hashes)} blocks from A")
    for hsh in reversed(hashes):
        blk = rpc(19195, {"op": "get_block", "hash": hsh})["block"]
        r = rpc(19196, {"op": "submit_block", "block": blk})
        assert r["ok"], r
    tb = rpc(19196, {"op": "get_tip"})
    print(f"B tip: height={tb['height']} {tb['tip'][:16]}..")
    assert tb["tip"] == a.chain.tip
    ok, msg = b.chain.verify()
    assert ok, msg
    print(f"B verify: {msg}")
    # tampered block rejected over the wire too
    evil = dict(rpc(19195, {"op": "get_block", "hash": a.chain.tip})["block"])
    evil["txs"] = evil["txs"] + [{"type": "mint", "to": "pq1mallory", "amount": 1}]
    r = rpc(19196, {"op": "submit_block", "block": evil})
    print(f"tampered gossip block: {'rejected' if not r['ok'] else 'ACCEPTED (BAD!)'} ({r['msg']})")
    assert not r["ok"]
    print("GOSSIP PASS: B converged on A's chain, tampering rejected.")


if __name__ == "__main__":
    main()
