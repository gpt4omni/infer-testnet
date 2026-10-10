#!/usr/bin/env python3
"""d1 judge drills: calibrated scores separate good from garbage, and every
inference served through the gateway carries a d1_quality stamp."""
import os
import sys
import threading
sys.path.insert(0, os.path.dirname(__file__))

from judge import DJudge
from ledger import connect
from miner import MockBackend
from net import GatewayServer, MinerClient


def main():
    j = DJudge()
    g = j.quality("write add(a,b)", "def solve(a, b):\n    return a + b\n")
    b = j.quality("write add(a,b)", "def solve(a, b):\n    return 42\n")
    print(f"good={g:.2f} garbage={b:.2f}")
    assert g is not None and b is not None, "judge down? start d1 server on 19092"
    assert g > 0.7 and b < 0.3, "no discrimination!"
    print("drill 1 CAUGHT apart: calibrated gap, not vibes")

    db = "/tmp/opencode/judge-demo.db"
    if os.path.exists(db):
        os.remove(db)
    srv = GatewayServer(connect(db, check_same_thread=False), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cl = MinerClient("judge-miner", MockBackend("good"), port=srv.port,
                     model="m", price=0.02, infer=True)
    import socket
    from net import send_msg, recv_msg
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    with s, s.makefile("rwb") as f:
        send_msg(f, {"op": "infer_submit", "prompt": "write a haiku about blocks",
                     "max_tokens": 64, "customer": "tester"})
        r = recv_msg(f)
        assert r["ok"], r
        jid = r["job_id"]
    kind, what, res = cl.run_mixed_once()
    assert kind == "infer" and res.get("ok"), (kind, res)
    cl.close()
    import time
    time.sleep(3)  # d1 scores synchronously in submit; margin for slow disks
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    with s, s.makefile("rwb") as f:
        send_msg(f, {"op": "infer_result", "job_id": jid})
        got = recv_msg(f)
    print("gateway result keys:", sorted(got.keys()))
    assert got.get("d1_quality") is not None, "missing d1 stamp!"
    print(f"drill 2 STAMPED: d1_quality={got['d1_quality']}")
    print("JUDGE DEMO PASS.")


if __name__ == "__main__":
    main()
