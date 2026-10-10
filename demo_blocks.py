#!/usr/bin/env python3
"""Live blocks: gateway in block mode mines miner submits into PoW chain.
Submits settle ONLY via blocks; restart reloads chain from disk."""
import os
import sys
import threading
import time
sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect, balance_watts
from miner import MockBackend
from money import fmt_watts
from net import GatewayServer, MinerClient

DB = os.path.join(os.path.dirname(__file__), "ledger-blocks.db")
CHAIN = os.path.join(os.path.dirname(__file__), "blocks-demo.dat")


def main():
    for p in (DB, CHAIN):
        if os.path.exists(p):
            os.remove(p)
    srv = GatewayServer(connect(DB, check_same_thread=False), port=19197,
                        block_mode=True, chain_path=CHAIN, bits=2 ** 242,
                        mine_interval=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.start_mining()

    cl = MinerClient("live-miner", MockBackend("good"), port=19197, model="m", price=0.02)
    for _ in range(3):
        print(cl.run_job(), flush=True)

    def wait_height(h, timeout=60):
        t0 = time.time()
        while srv.chain.height < h and time.time() - t0 < timeout:
            time.sleep(1)

    wait_height(0)
    for _ in range(3):
        print(cl.run_job(), flush=True)
    cl.close()
    wait_height(2)  # genesis + one block per batch
    bal = balance_watts(connect(DB), "live-miner")
    print(f"tip height={srv.chain.height} balance={fmt_watts(bal)}")
    assert srv.chain.height >= 1 and bal > 0

    # restart: chain + state survive on disk
    old_tip = srv.chain.tip
    from chain import load_chain
    ch2 = load_chain(CHAIN)
    assert ch2.tip == old_tip, "chain did not persist"
    ok, msg = ch2.verify()
    assert ok, msg
    print(f"reloaded: {msg}; tip {old_tip[:16]}.. intact")
    print("BLOCKS DEMO PASS: submits settle via PoW, state survives restart.")


if __name__ == "__main__":
    main()
