#!/usr/bin/env python3
"""Infer customer: submit open prompts, get answers. Payment is automatic:
d1 scores the delivery and good work settles on its own — no accept button,
no free sampling. This CLI only reports + rates (feedback op)."""
import argparse
import json
import os
import socket
import sys
import time
sys.path.insert(0, os.path.dirname(__file__))


def rpc(host, port, obj, timeout=20, pin=""):
    if pin:
        import pqchannel as _pq
        pub = _pq.fetch_server_key(host, port)
        import socket as _s
        raw = _s.create_connection((host, port), timeout=timeout)
        s = _pq.client_connect(raw, pub, pin)
    else:
        import socket
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
    ap.add_argument("--rate", default="auto", choices=["auto", "ask", "none"],
                    help="feedback rating only; payment is automatic on d1 score")
    ap.add_argument("--rate-min-quality", type=float, default=0.3)
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
    if res.get("paid_watts"):
        from money import fmt_watts
        print(f"auto-settled {fmt_watts(res['paid_watts'])} to miner (d1 gate passed).")
    else:
        print("below pay line — miner unpaid.")

    rate = False
    if a.rate == "auto":
        q = res.get("d1_quality")
        rate = (q is not None and q >= a.rate_min_quality)
        print(f"rating: {'good' if rate else 'bad'} (d1={q})")
    elif a.rate == "ask":
        rate = input("rate this answer good? [y/N] ").strip().lower() == "y"
    call({"op": "feedback", "miner_id": res.get("miner", ""), "accept": bool(rate)})
    print("feedback recorded.")


if __name__ == "__main__":
    main()
