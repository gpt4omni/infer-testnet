#!/usr/bin/env python3
"""Infer customer: submit open prompts, review answers, pay on accept.

  python3 customer_node.py --gateway 127.0.0.1:19191 --no-tls --keyfile c.json \\
      --prompt "Write a haiku about blocks" --accept ask

Flow: quote -> poll result -> show answer + d1 score -> accept? pay : walk.
Pay = PQ-signed transfer (quoted watts) into the mempool + feedback op.
Reject = feedback(false), miner unpaid, stats dinged. No escrow, no trust:
money moves only after the customer says so.
"""
import argparse
import json
import os
import socket
import sys
import time
sys.path.insert(0, os.path.dirname(__file__))


def rpc(host, port, obj, timeout=20, pin=""):
    if pin:
        from net import tls_connect
        s = tls_connect(host, port, pin, timeout)
    else:
        s = socket.create_connection((host, port), timeout=timeout)
    from net import send_msg, recv_msg
    with s, s.makefile("rwb") as f:
        send_msg(f, obj)
        return recv_msg(f)


def load_keyfile(path):
    with open(path) as f:
        d = json.load(f)
    if "enc" in d:
        pw = os.environ.get("INFER_WALLET_PW")
        if not pw:
            raise SystemExit("encrypted keyfile needs INFER_WALLET_PW")
        from wallet import _fernet
        return json.loads(_fernet(pw, bytes.fromhex(d["salt"])).decrypt(d["enc"].encode()).decode())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--pin", default="")
    ap.add_argument("--no-tls", action="store_true")
    ap.add_argument("--keyfile", required=True)
    ap.add_argument("--prompt", default="", help="prompt text ('-' reads stdin)")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--accept", default="ask", choices=["ask", "auto", "never"])
    ap.add_argument("--auto-min-quality", type=float, default=0.3)
    ap.add_argument("--wait", type=int, default=180)
    a = ap.parse_args()

    prompt = sys.stdin.read().strip() if a.prompt == "-" else a.prompt.strip()
    if not prompt:
        raise SystemExit("empty prompt")
    host, port = a.gateway.rsplit(":", 1)
    port = int(port)
    pin = "" if a.no_tls else a.pin
    key = load_keyfile(a.keyfile)

    def call(o, timeout=20):
        return rpc(host, port, o, timeout, pin)

    import pq as _pq
    call({"op": "register_pq", "addr": key["addr"],
          "root_hex": _pq.root_of(bytes.fromhex(key["seed"])).hex()})
    r = call({"op": "infer_submit", "prompt": prompt, "max_tokens": a.max_tokens,
              "customer": key["addr"]})
    if not r.get("ok"):
        raise SystemExit(f"submit rejected: {r}")
    jid, quote = r["job_id"], r["quoted_watts"]
    print(f"job {jid} quoted {quote} watts — waiting for miners…", flush=True)

    t0 = time.time()
    res = None
    while time.time() - t0 < a.wait:
        time.sleep(3)
        res = call({"op": "infer_result", "job_id": jid})
        if res.get("ok"):
            break
    if not res or not res.get("ok"):
        print("no miner served it in time. not paid.")
        return
    print(f"--- answer (by {res.get('miner','?')[:16]}.., d1={res.get('d1_quality', '?')}) ---")
    print(res.get("output", ""))
    print("--- end ---")

    accept = False
    if a.accept == "auto":
        q = res.get("d1_quality")
        accept = (q is not None and q >= a.auto_min_quality)
        print(f"auto: d1={q} threshold={a.auto_min_quality} -> {'accept' if accept else 'reject'}")
    elif a.accept == "ask":
        accept = input("accept and pay? [y/N] ").strip().lower() == "y"
    call({"op": "feedback", "miner_id": res.get("miner", ""), "accept": bool(accept)})

    if not accept:
        print("rejected — miner unpaid, stats dinged.")
        return
    import pq
    from money import fmt_watts
    n = call({"op": "next_ots", "miner_id": key["addr"]})["ots"]
    pkg = pq.sign(key["seed"], n, key["addr"], res["miner"], quote)
    tx = {"type": "transfer", "from": key["addr"], "to": res["miner"], "amount": quote, "sig": pkg}
    s = call({"op": "submit_tx", "tx": tx})
    if s.get("ok"):
        print(f"paid {fmt_watts(quote)} — confirms in next block.")
    else:
        print(f"payment rejected: {s.get('msg')} (feedback stands)")


if __name__ == "__main__":
    main()
