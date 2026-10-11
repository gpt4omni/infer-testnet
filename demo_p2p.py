#!/usr/bin/env python3
"""Two miners, one gateway, real TCP: honest earns over the wire, cheat earns 0."""
import os
import sys
import tempfile
import threading
sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect, balance_watts
from miner import MockBackend
from money import fmt_watts
from net import GatewayServer, MinerClient

DB = os.path.join(os.path.dirname(__file__), "ledger-p2p.db")


def client(name, kind, n, out, port=19191):
    from miner import MockBackend as MB
    cl = MinerClient(name, MB(kind), port=port, model="mock", price=0.02 if kind == "good" else 0.002)
    earned = 0
    for _ in range(n):
        task, res = cl.run_job()
        earned += res["payout_watts"]
        out.append((name, task, res["passed"], res["payout_watts"]))
    cl.close()


def main():
    with tempfile.TemporaryDirectory(prefix="infer-p2p-") as directory:
        run_proof(directory)


def run_proof(directory):
    import sys as _sys
    port = int(_sys.argv[1]) if len(_sys.argv) > 1 else 0
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    srv = GatewayServer(connect(DB, check_same_thread=False), port=port,
                        chain_path=os.path.join(directory, "chain.dat"))
    port = srv.port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    rows: list = []
    ts = [threading.Thread(target=client, args=("net-good", "good", 4, rows, port)),
          threading.Thread(target=client, args=("net-cheat", "cheat", 4, rows, port))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    for name, task, passed, pay in sorted(rows):
        print(f"{name:10s} {task:8s} pass={int(passed)} +{fmt_watts(pay)}")
    print(f"net-good balance: {fmt_watts(balance_watts(c, 'net-good'))}")
    print(f"net-cheat balance: {fmt_watts(balance_watts(c, 'net-cheat'))}")
    assert balance_watts(c, "net-good") > 0 and balance_watts(c, "net-cheat") == 0
    print("P2P OK: wire transport, same scoring, cheat still earns 0.")
    srv.close()
    srv.conn.close()
    c.close()


if __name__ == "__main__":
    main()
