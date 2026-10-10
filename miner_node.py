#!/usr/bin/env python3
"""Infer miner node: connects to a gateway, serves jobs for watts.

  python3 miner_node.py --gateway 192.168.50.219:19191 --pin <PIN> --keyfile m1.json --jobs 20
  python3 miner_node.py --gateway 127.0.0.1:19191 --pin <PIN> --keyfile m1.json --backend gpu --jobs 0  # forever

Keyfile: {"seed": hex, "addr": "pq1..."} (or wallet-pq.json + INFER_WALLET_PW).
Submits are PQ-signed per job; gateway verifies + rejects OTS reuse.
"""
import argparse
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))


def load_keyfile(path):
    with open(path) as f:
        d = json.load(f)
    if "enc" in d:
        pw = os.environ.get("INFER_WALLET_PW")
        if not pw:
            raise SystemExit("encrypted keyfile needs INFER_WALLET_PW")
        sys.path.insert(0, os.path.dirname(__file__))
        from wallet import _fernet
        return json.loads(_fernet(pw, bytes.fromhex(d["salt"])).decrypt(d["enc"].encode()).decode())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True, help="host:port")
    ap.add_argument("--pin", default="", help="gateway cert pin (required for TLS)")
    ap.add_argument("--no-tls", action="store_true", help="plaintext (loopback tests only)")
    ap.add_argument("--keyfile", required=True)
    ap.add_argument("--backend", default="mock", choices=["mock", "cheat", "gpu"])
    ap.add_argument("--jobs", type=int, default=10, help="0 = forever")
    ap.add_argument("--price", type=float, default=0.02)
    a = ap.parse_args()

    host, port = a.gateway.rsplit(":", 1)
    key = load_keyfile(a.keyfile)
    if a.backend == "gpu":
        from miner import LlamaCppBackend
        be, model = LlamaCppBackend(), "gemma-4-12B-Q4"
    elif a.backend == "cheat":
        from miner import MockBackend
        be, model = MockBackend("cheat"), "tiny-0.5b"
    else:
        from miner import MockBackend
        be, model = MockBackend("good"), "mock-7b"

    from money import fmt_watts
    if a.no_tls:
        from net import MinerClient
        print("WARNING: plaintext transport (loopback only)", flush=True)
        # plain client has no signed submits; WAN must use TLS+WANMiner
        cl = MinerClient(key["addr"], be, host, int(port), model, a.price)
        run = cl.run_job
    else:
        if not a.pin:
            raise SystemExit("--pin required (gateway prints it at startup)")
        from net import WANMiner
        cl = WANMiner(key, be, host, int(port), a.pin, model, a.price)
        run = cl.run_job

    earned, n = 0, 0
    try:
        while a.jobs == 0 or n < a.jobs:
            task, res = run()
            if res.get("ok") is False:
                print(f"REJECTED: {res}", flush=True)
                break
            earned += res.get("payout_watts", 0)
            n += 1
            print(f"job {n}: {task} pass={int(res.get('passed', 0))} "
                  f"+{fmt_watts(res.get('payout_watts', 0))} total={fmt_watts(earned)}", flush=True)
    finally:
        cl.close()
    print(f"done: {n} jobs, {fmt_watts(earned)}")


if __name__ == "__main__":
    main()
