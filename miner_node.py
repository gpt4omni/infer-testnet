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
import time
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


def mine_forever(a):
    """Open block production: pull template + mempool, assemble split
    coinbase to own key, grind PoW, submit. Anyone can do this — that is
    what makes production permissionless instead of operator-only."""
    import time as _t
    from net import gateway_rpc

    def rpc(obj, timeout=30):
        return gateway_rpc(a.gateway, obj, "" if a.no_tls else a.pin, timeout)

    from chain import mine_block, split_coinbase, MAX_BLOCK_REWARD_WATTS
    key = load_keyfile(a.keyfile)
    n = 0
    print(f"block-mining as {key['addr'][:20]}.. (Ctrl-C to stop)", flush=True)
    while a.mine_max_blocks == 0 or n < a.mine_max_blocks:
        try:
            t = rpc({"template": None, "op": "template"})
            m = rpc({"op": "get_mempool"})
        except (ConnectionError, OSError, ValueError) as e:
            print(f"template fetch failed: {e}; retrying…", flush=True)
            _t.sleep(5)
            continue
        if t["prev_hash"] == "00" * 32 and t["height"] == 0:
            print("no genesis yet — mining it (empty, no coinbase by consensus rule)…", flush=True)
            from chain import mine_block as _gen
            g = _gen("00" * 32, 0, t["bits"], [])
            try:
                r = rpc({"op": "submit_block", "block": g}, timeout=60)
                print(f"genesis: {r.get('msg')} tip={r.get('height')}", flush=True)
            except (ConnectionError, OSError, ValueError) as e:
                print(f"submit failed: {e}", flush=True)
            _t.sleep(2)
            continue
        txs = m.get("txs", [])
        coin = split_coinbase(min(m.get("cap_watts", MAX_BLOCK_REWARD_WATTS), MAX_BLOCK_REWARD_WATTS),
                              key["addr"], txs, height=t["height"])
        blk = mine_block(t["prev_hash"], t["height"], t["bits"], coin + txs)
        try:
            r = rpc({"op": "submit_block", "block": blk}, timeout=60)
        except (ConnectionError, OSError, ValueError) as e:
            print(f"submit failed: {e}", flush=True)
            continue
        n += 1
        print(f"block {t['height']}: {r.get('msg')} tip={r.get('height')} ({len(txs)} mempool txs)", flush=True)
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True, help="host:port")
    ap.add_argument("--pin", default="", help="gateway KEM pin (required for PQ transport)")
    ap.add_argument("--no-tls", action="store_true", help="plaintext (loopback tests only)")
    ap.add_argument("--keyfile", required=True)
    ap.add_argument("--backend", default="mock", choices=["mock", "cheat", "gpu"])
    ap.add_argument("--jobs", type=int, default=10, help="0 = forever")
    ap.add_argument("--price", type=float, default=0.02)
    ap.add_argument("--mine", action="store_true",
                    help="produce blocks too (open production): fetch mempool, grind PoW, submit")
    ap.add_argument("--mine-max-blocks", type=int, default=0, help="0 = forever")
    ap.add_argument("--infer", action="store_true",
                    help="serve open-prompt inference jobs as well as tasks")
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
    if not a.no_tls and not a.pin:
        raise SystemExit("--pin required (gateway prints its KEM pin at startup)")
    if a.mine:
        mine_forever(a)
        return
    def connect():
        if a.no_tls:
            from net import MinerClient
            return MinerClient(key["addr"], be, host, int(port), model, a.price,
                               infer=a.infer), "plain"
        from net import WANMiner
        return WANMiner(key, be, host, int(port), a.pin, model, a.price,
                        infer=a.infer), "wan"

    if a.no_tls:
        print("WARNING: plaintext transport (loopback only)", flush=True)
    elif not a.pin:
        raise SystemExit("--pin required (gateway prints it at startup)")
    cl, kind = connect()
    box = [cl]

    def run():
        if a.infer:
            knd, what, res = box[0].run_mixed_once()
            return (f"infer:{what}" if knd == "infer" else what), res
        return box[0].run_job()

    def reconnect():
        try:
            box[0].close()
        except Exception:
            pass
        box[0], _ = connect()

    earned, n = 0, 0
    try:
        while a.jobs == 0 or n < a.jobs:
            try:
                task, res = run()
            except (ConnectionError, OSError, TimeoutError) as e:
                print(f"net blip ({e}) — reconnecting in 5s…", flush=True)
                time.sleep(5)
                try:
                    reconnect()
                except Exception as e2:
                    print(f"reconnect failed ({e2}) — retrying…", flush=True)
                continue
            if res.get("ok") is False:
                if "rate limited" in str(res.get("msg", "")):
                    print("throttled — backing off 10s…", flush=True)
                    time.sleep(10)
                    continue
                print(f"REJECTED: {res}", flush=True)
                break
            earned += res.get("payout_watts", 0)
            n += 1
            print(f"job {n}: {task} pass={int(res.get('passed', 1))} "
                  f"+{fmt_watts(res.get('payout_watts', 0))} total={fmt_watts(earned)}", flush=True)
    finally:
        try:
            box[0].close()
        except Exception:
            pass
    print(f"done: {n} jobs, {fmt_watts(earned)}")


if __name__ == "__main__":
    main()
