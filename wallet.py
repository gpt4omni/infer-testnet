#!/usr/bin/env python3
"""Infer test wallet: mine (mock or real 1080 Ti) + hold + send watts.

Test funds only. Balances = ledger payouts + transfers.
Run: ./wallet.sh   (sets LD_LIBRARY_PATH for local Tcl/Tk 8.6)
"""
import os
import json
import queue
import random
import sqlite3
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect, register_miner, register_pq, next_ots, transfer, balance_watts, leaderboard
from gateway import Miner, dispatch
from miner import MockBackend
from money import fmt_watts, to_watts, to_infr, TICKER
from eval import TASKS
import pq

DB = os.path.join(os.path.dirname(__file__), "wallet.db")
KEYFILE = os.path.join(os.path.dirname(__file__), "wallet-pq.json")
BACKENDS = {"Tiny 0.5B (fast GPU)": "tiny", "Mock 7B (instant)": "mock"}
GPU_URL = "http://127.0.0.1:19091/health"


def paths(profile: str):
    """Separate DB + key per profile so two wallets never share state.
    Frozen builds use ~/.local/share/infer-wallet (bundle is read-only)."""
    from ledger import app_state_dir
    base = app_state_dir() if getattr(sys, "frozen", False) else os.path.dirname(__file__)
    if profile in ("", "default"):
        return os.path.join(base, "wallet.db"), os.path.join(base, "wallet-pq.json")
    return (os.path.join(base, f"wallet-{profile}.db"),
            os.path.join(base, f"wallet-{profile}-pq.json"))


def _fernet(password: str, salt: bytes):
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.fernet import Fernet
    raw = PBKDF2HMAC(hashes.SHA256(), 32, salt, 600_000).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(raw))


def _ask_password(prompt: str) -> str | None:
    if os.environ.get("INFER_WALLET_PW"):
        return os.environ["INFER_WALLET_PW"]
    try:
        from tkinter import simpledialog
        return simpledialog.askstring("Wallet password", prompt, show="*")
    except Exception:
        return None


def load_key(keyfile=None):
    """Password-encrypted seed (Fernet/PBKDF2, 0600). Migrates legacy
    plaintext files on first unlock. Returns (key, password)."""
    kf = keyfile or KEYFILE
    if os.path.exists(kf):
        with open(kf) as f:
            data = json.load(f)
        if "enc" not in data:  # legacy plaintext -> encrypt now
            pw = _ask_password("Set a password for your wallet key:")
            if not pw:
                return {"seed": data["seed"], "addr": data["addr"]}, None
            save_key(data, pw, kf)
            return data, pw
        pw = _ask_password("Wallet password:")
        if not pw:
            raise SystemExit("password required")
        f_ = _fernet(pw, bytes.fromhex(data["salt"]))
        return json.loads(f_.decrypt(data["enc"].encode()).decode()), pw
    pw = _ask_password("Set a password for your NEW wallet key:")
    if not pw:
        raise SystemExit("password required to create wallet")
    k = pq.generate()
    save_key(k, pw, kf)
    return k, pw


def save_key(key: dict, password: str | None, keyfile=None):
    kf = keyfile or KEYFILE
    if password is None:  # non-interactive fallback (tests); file still 0600
        with open(kf, "w") as f:
            json.dump(key, f)
    else:
        import secrets as _secrets
        salt = _secrets.token_bytes(16)
        tok = _fernet(password, salt).encrypt(json.dumps(key).encode()).decode()
        with open(kf, "w") as f:
            json.dump({"enc": tok, "salt": salt.hex(), "addr": key["addr"]}, f)
    os.chmod(kf, 0o600)


def gpu_alive() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(GPU_URL, timeout=4)
        return True
    except Exception:
        return False


class Wallet:
    def __init__(self, root, profile="default", gw=None):
        self.root = root
        self.profile = profile
        self.gw = gw  # None = local mode; else dict(host, port, pin, tls)
        self.db_path, self.keyfile = paths(profile)
        mode = f"via {gw['host']}:{gw['port']}" if gw else "local"
        self._mode = mode
        root.title(f"Infer Wallet [{profile}] {mode} — TESTNET ({TICKER})")
        root.geometry("640x560")
        try:
            style = ttk.Style()
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        self.conn = connect(self.db_path)
        self.key, self._pw = load_key(self.keyfile)
        register_pq(self.conn, self.key["addr"], pq.root_of(bytes.fromhex(self.key["seed"])).hex())
        self.mining = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.session_earned = 0
        self.session_jobs = 0
        self.net_cache = {"bal": None, "left": None, "jobs": [], "txs": []}
        self._fetching = False
        self._last_fetch = 0.0

        # header ------------------------------------------------------
        hdr = ttk.Frame(root, padding=(10, 8, 10, 0))
        hdr.grid(sticky="ew")
        hdr.columnconfigure(0, weight=1)
        ttk.Label(hdr, text="◈ Infer Wallet", font=("", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(hdr, text="TESTNET — no real funds", foreground="#a60",
                  font=("", 9, "bold")).grid(row=0, column=1, sticky="e")

        # account card -------------------------------------------------
        acct = ttk.LabelFrame(root, text="Account", padding=8)
        acct.grid(sticky="ew", padx=10, pady=6)
        acct.columnconfigure(1, weight=1)
        ttk.Label(acct, text="Address").grid(row=0, column=0, sticky="w")
        self.mid = tk.StringVar(value=self.key["addr"])
        ttk.Entry(acct, textvariable=self.mid, width=40, state="readonly",
                  font=("TkFixedFont",)).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(acct, text="Copy", width=6, command=self.copy_addr).grid(row=0, column=2)
        ttk.Label(acct, text="Backend").grid(row=1, column=0, sticky="w", pady=2)
        brow = ttk.Frame(acct)
        brow.grid(row=1, column=1, sticky="w", padx=4)
        self.backend = tk.StringVar(value="Tiny 0.5B (fast GPU)")
        ttk.OptionMenu(brow, self.backend, "Tiny 0.5B (fast GPU)", *BACKENDS,
                       command=lambda *_: self.refresh_gpu()).pack(side="left")
        self.gpu_dot = tk.StringVar(value="●")
        ttk.Label(brow, textvariable=self.gpu_dot, font=("", 12)).pack(side="left", padx=6)
        self.gpu_txt = tk.StringVar(value="gpu: …")
        ttk.Label(brow, textvariable=self.gpu_txt, font=("", 9)).pack(side="left")
        self.bal = tk.StringVar(value="…")
        ttk.Label(acct, textvariable=self.bal, font=("", 15, "bold")).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        otsrow = ttk.Frame(acct)
        otsrow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=2)
        otsrow.columnconfigure(1, weight=1)
        ttk.Label(otsrow, text="Sig keys", font=("", 9)).grid(row=0, column=0)
        self.ots_bar = ttk.Progressbar(otsrow, maximum=pq.NKEYS, mode="determinate", length=200)
        self.ots_bar.grid(row=0, column=1, sticky="ew", padx=6)
        self.ots_txt = tk.StringVar(value="")
        ttk.Label(otsrow, textvariable=self.ots_txt, font=("", 9)).grid(row=0, column=2)

        # actions -------------------------------------------------------
        btns = ttk.Frame(root, padding=(10, 0))
        btns.grid(sticky="ew")
        self.mine_btn = ttk.Button(btns, text="▶ Start mining", command=self.toggle_mine)
        self.mine_btn.grid(row=0, column=0, padx=(0, 4))
        ttk.Button(btns, text="Faucet +1 INFR", command=self.faucet).grid(row=0, column=1, padx=2)
        ttk.Button(btns, text="Leaderboard", command=self.show_board).grid(row=0, column=2, padx=2)
        ttk.Button(btns, text="Verify me", command=self.verify_me).grid(row=0, column=3, padx=2)
        ttk.Button(btns, text="Rotate key", command=self.rotate).grid(row=0, column=4, padx=2)
        ttk.Button(btns, text="Backup seed", command=self.backup).grid(row=0, column=5, padx=2)
        self.spin = ttk.Progressbar(btns, mode="indeterminate", length=80)

        # send card ------------------------------------------------------
        send = ttk.LabelFrame(root, text="Send watts (PQ-signed)", padding=8)
        send.grid(sticky="ew", padx=10, pady=6)
        send.columnconfigure(1, weight=1)
        ttk.Label(send, text="To").grid(row=0, column=0, sticky="w")
        self.to_id = tk.StringVar()
        ttk.Entry(send, textvariable=self.to_id, width=40,
                  font=("TkFixedFont",)).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(send, text="Watts").grid(row=1, column=0, sticky="w", pady=2)
        self.amount = tk.StringVar(value="100")
        self.amount.trace_add("write", lambda *_: self.amount_hint())
        ttk.Entry(send, textvariable=self.amount, width=14).grid(row=1, column=1, sticky="w", padx=4)
        self.amt_hint = tk.StringVar(value="")
        ttk.Label(send, textvariable=self.amt_hint, font=("", 9)).grid(row=1, column=2, sticky="w")
        ttk.Button(send, text="Send", command=self.send).grid(row=0, column=2, rowspan=2, padx=4)

        # tabs ------------------------------------------------------------
        nb = ttk.Notebook(root)
        nb.grid(sticky="nsew", padx=10, pady=(0, 4))
        root.rowconfigure(5, weight=1)
        root.columnconfigure(0, weight=1)
        self.log = tk.Text(nb, height=12, width=70, state="disabled",
                           font=("TkFixedFont", 9), wrap="word")
        self.log.tag_config("good", foreground="#0a5")
        self.log.tag_config("bad", foreground="#c00")
        nb.add(self.log, text="Activity")
        self.jobs_tv = ttk.Treeview(nb, columns=("task", "res", "pay", "lat"), show="headings", height=8)
        for c_, w_ in (("task", 90), ("res", 60), ("pay", 110), ("lat", 80)):
            self.jobs_tv.heading(c_, text=c_.title())
            self.jobs_tv.column(c_, width=w_)
        nb.add(self.jobs_tv, text="My jobs")
        self.tx_tv = ttk.Treeview(nb, columns=("dir", "peer", "amt"), show="headings", height=8)
        for c_, w_ in (("dir", 60), ("peer", 300), ("amt", 130)):
            self.tx_tv.heading(c_, text=c_.title())
            self.tx_tv.column(c_, width=w_)
        nb.add(self.tx_tv, text="Transfers")
        pg = ttk.Frame(nb, padding=8)
        nb.add(pg, text="Playground")
        pg.columnconfigure(0, weight=1)
        ttk.Label(pg, text="Ask anything — served by network miners, scored by d1.").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.pg_prompt = tk.Text(pg, height=4, width=60, wrap="word", font=("", 10))
        self.pg_prompt.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        btnrow = ttk.Frame(pg)
        btnrow.grid(row=2, column=0, columnspan=2, sticky="w")
        self.pg_ask_btn = ttk.Button(btnrow, text="Ask", command=self.ask_pg)
        self.pg_ask_btn.pack(side="left")
        self.pg_pay_btn = ttk.Button(btnrow, text="Accept & pay", command=self.pay_pg, state="disabled")
        self.pg_pay_btn.pack(side="left", padx=6)
        self.pg_meta = tk.StringVar(value="")
        ttk.Label(btnrow, textvariable=self.pg_meta, font=("", 9)).pack(side="left", padx=6)
        self.pg_answer = tk.Text(pg, height=10, width=60, wrap="word", state="disabled",
                                 font=("TkFixedFont", 9))
        self.pg_answer.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=4)
        pg.rowconfigure(3, weight=1)
        self.pg_job = None  # {job_id, miner, quote, output, score}
        root.rowconfigure(4, weight=1)

        # status bar -------------------------------------------------------
        self.status = tk.StringVar(value="idle")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w",
                  padding=(6, 2)).grid(sticky="ew", padx=10, pady=(0, 8))

        self.refresh()
        self.refresh_gpu()
        self.amount_hint()
        root.after(500, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- network mode (shared chain via gateway) ---------------------------
    def gw_rpc(self, obj, timeout=15):
        import socket
        from net import send_msg, recv_msg
        if self.gw.get("tls"):
            from net import tls_connect
            s = tls_connect(self.gw["host"], self.gw["port"], self.gw["pin"], timeout)
        else:
            s = socket.create_connection((self.gw["host"], self.gw["port"]), timeout=timeout)
        with s, s.makefile("rwb") as f:
            send_msg(f, obj)
            return recv_msg(f)

    def gw_ots(self, addr: str) -> int:
        return int(self.gw_rpc({"op": "next_ots", "miner_id": addr})["ots"])

    def _backend(self):
        if BACKENDS[self.backend.get()] == "tiny":
            if not gpu_alive():
                self.say("Tiny server down at 19091 — mining mock instead.", "bad")
                return MockBackend("good"), "mock-7b", 0.02
            from miner import LlamaCppBackend
            return LlamaCppBackend(model="qwen2.5-0.5b-Q4"), "qwen2.5-0.5b-Q4", 0.005
        return MockBackend("good"), "mock-7b", 0.02

    def net_miner(self, mid: str, backend_kind: str):
        be, model, price = self._backend_kind(backend_kind)
        if self.gw.get("tls"):
            from net import WANMiner
            return WANMiner(self.key, be, self.gw["host"], self.gw["port"], self.gw["pin"],
                            model, price), "wan"
        from net import MinerClient
        return MinerClient(mid, be, self.gw["host"], self.gw["port"], model, price), "plain"

    def _backend_kind(self, kind: str):
        if kind == "tiny" and gpu_alive():
            from miner import LlamaCppBackend
            return LlamaCppBackend(model="qwen2.5-0.5b-Q4"), "qwen2.5-0.5b-Q4", 0.005
        return MockBackend("good"), "mock-7b", 0.02

    # -- actions (main thread) -------------------------------------------
    def miner(self):
        mid = self.mid.get().strip() or self.key["addr"]
        be, model, price = self._backend()
        return Miner(mid, be, model, price)

    def copy_addr(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.mid.get())
        self.say("Address copied to clipboard.")

    def refresh_gpu(self):
        alive = gpu_alive()
        self.gpu_txt.set("gpu: online" if alive else "gpu: offline")
        return alive

    def ensure_registered(self):
        try:
            import pq as _pq
            self.gw_rpc({"op": "register_pq", "addr": self.key["addr"],
                         "root_hex": _pq.root_of(bytes.fromhex(self.key["seed"])).hex()})
        except (ConnectionError, OSError, ValueError, KeyError):
            pass

    def faucet(self):
        if self.gw:
            self.say(f"No local faucet in network mode — ask the gateway op to run:\ntestnet_faucet.py {self.mid.get().strip()} 1", "bad")
            return
        try:
            transfer(self.conn, "faucet", self.mid.get().strip(), to_watts(1))
            self.say("Faucet: +1.000 INFR (test funds).", "good")
        except ValueError as e:
            self.say(f"Faucet failed: {e}", "bad")
        self.refresh()

    def send(self):
        try:
            amount = int(self.amount.get())
        except ValueError:
            self.say("Send failed: amount must be integer watts.", "bad")
            return
        frm, to = self.mid.get().strip(), self.to_id.get().strip()
        if not to:
            self.say("Send failed: recipient empty.", "bad")
            return
        if frm != self.key["addr"]:
            self.say("Send failed: active id is not this wallet's PQ key.", "bad")
            return
        if not messagebox.askyesno("Confirm send", f"Send {fmt_watts(amount)} to\n{to}?"):
            return
        if self.gw:
            self.say("Submitting transfer…")
            threading.Thread(target=self._net_send, args=(frm, to, amount), daemon=True).start()
            return
        try:
            pkg = pq.sign(self.key["seed"], next_ots(self.conn, frm), frm, to, amount)
            transfer(self.conn, frm, to, amount, pkg)
            try:
                import gossip
                gossip.broadcast(frm, pq.root_of(bytes.fromhex(self.key["seed"])).hex(),
                                 to, amount, pkg)
            except OSError as e:
                self.say(f"Sent locally but broadcast failed: {e}", "bad")
            self.say(f"Sent {fmt_watts(amount)} to {to[:20]}… (OTS #{pkg['ots']}).", "good")
        except (ValueError, sqlite3.Error) as e:
            self.say(f"Send failed: {e}", "bad")
        self.refresh()

    def _net_send(self, frm: str, to: str, amount: int):
        try:
            self.ensure_registered()
            for attempt in (0, 1):  # retry once with fresh OTS if mempool raced us
                pkg = pq.sign(self.key["seed"], self.gw_ots(frm), frm, to, amount)
                tx = {"type": "transfer", "from": frm, "to": to, "amount": amount, "sig": pkg}
                r = self.gw_rpc({"op": "submit_tx", "tx": tx})
                if r.get("ok") or "OTS" not in str(r.get("msg", "")):
                    break
            self.events.put(("info", f"Network send: {r.get('msg', r)} (confirms in next block)."))
        except (ValueError, ConnectionError, OSError, KeyError) as e:
            self.events.put(("info", f"Send failed: {e}"))

    def amount_hint(self):
        try:
            self.amt_hint.set(f"= {to_infr(int(self.amount.get())):.6f} {TICKER}")
        except ValueError:
            self.amt_hint.set("")

    def toggle_mine(self):
        if self.mining.is_set():
            self.stop()
        else:
            self.start()

    def start(self):
        if self.mining.is_set():
            return
        if self.gw:
            self.mining.set()
            mid = self.mid.get().strip() or self.key["addr"]
            kind = BACKENDS[self.backend.get()]
            threading.Thread(target=self._net_connect_loop, args=(mid, kind), daemon=True).start()
            self.mine_btn.configure(text="⏸ Stop mining")
            self.spin.grid(row=0, column=6, padx=4)
            self.spin.start(30)
            self.status.set(f"connecting {self._mode}…")
            self.say(f"Connecting {self._mode}…")
            return
        m = self.miner()
        if m is None:
            return
        register_miner(self.conn, m.mid, m.model, m.price)
        self.mining.set()
        threading.Thread(target=self.loop, args=(m,), daemon=True).start()
        self.mine_btn.configure(text="⏸ Stop mining")
        self.spin.grid(row=0, column=6, padx=4)
        self.spin.start(30)
        self.status.set(f"mining as {m.mid[:16]}… ({m.model})")
        self.say(f"Mining as {m.mid[:16]}… ({m.model}).")

    def stop(self):
        self.mining.clear()
        self.say("Miner stopping…")

    def _net_connect_loop(self, mid: str, backend_kind: str):
        try:
            cl, kind = self.net_miner(mid, backend_kind)
        except (AssertionError, ConnectionError, ValueError, OSError) as e:
            self.events.put(("info", f"Gateway connect failed: {e}"))
            self.mining.clear()
            return
        try:
            self.ensure_registered()
        except (ConnectionError, OSError, ValueError, KeyError):
            pass
        self.events.put(("info", f"Mining {self._mode} ({kind})."))
        self.net_loop(cl)

    def show_board(self):
        self.say("--- leaderboard ---")
        for mid, n, psd, pay, avg in leaderboard(self.conn)[:8]:
            self.say(f"{mid[:20]}… jobs={n} pass={psd} earned={fmt_watts(pay or 0)}")

    def verify_me(self):
        from verify import CapabilityProfile
        m = self.miner()
        if m is None:
            return
        self.say("Spot-checking own backend on fresh canaries…")
        def run():
            try:
                p = CapabilityProfile()
                p.register("tmp", m.backend, seed="wallet-calib")
                ok, dist, vec = p.spot_check("tmp", m.backend, seed="wallet-check")
                self.events.put(("info", f"profile {tuple(round(x, 2) for x in vec)} "
                                 f"dist={dist:.2f} -> "
                                 f"{'HEALTHY' if ok else 'FAILING — do not mine with this backend'}"))
            except Exception as e:
                self.events.put(("info", f"verify error: {e}"))
        threading.Thread(target=run, daemon=True).start()

    def rotate(self):
        """OTS keys are single-use (256/wallet). Rotation moves the full
        balance to a fresh key before exhaustion bricks the wallet."""
        if not messagebox.askyesno("Rotate key", "Move full balance to a fresh PQ key?\nOld key is archived."):
            return
        try:
            old = self.key
            bal = balance_watts(self.conn, old["addr"])
            new = pq.generate()
            register_pq(self.conn, new["addr"], pq.root_of(bytes.fromhex(new["seed"])).hex())
            if bal > 0:
                pkg = pq.sign(old["seed"], next_ots(self.conn, old["addr"]), old["addr"], new["addr"], bal)
                transfer(self.conn, old["addr"], new["addr"], bal, pkg)
            arch = os.path.join(os.path.dirname(self.db_path), f"wallet-pq.old-{old['addr'][:10]}.json")
            with open(arch, "w") as f:
                json.dump(old, f)
            os.chmod(arch, 0o600)
            save_key(new, self._pw, self.keyfile)
            self.key = new
            self.mid.set(new["addr"])
            self.say(f"Rotated: {fmt_watts(bal)} moved to {new['addr'][:16]}…", "good")
        except (ValueError, sqlite3.Error, OSError) as e:
            self.say(f"Rotate failed: {e}", "bad")
        self.refresh()

    def backup(self):
        top = tk.Toplevel(self.root)
        top.title("Seed backup (TESTNET ONLY)")
        tk.Label(top, text="Anyone with this seed spends your funds. Never share it.",
                 fg="red").pack(padx=10, pady=4)
        t = tk.Text(top, height=3, width=70, font=("TkFixedFont", 9))
        t.pack(padx=10, pady=4)
        t.insert("end", self.key["seed"])
        t.configure(state="disabled")

    # -- inference playground ----------------------------------------------
    def pg_show(self, text, meta=""):
        self.pg_answer.configure(state="normal")
        self.pg_answer.delete("1.0", "end")
        self.pg_answer.insert("end", text)
        self.pg_answer.configure(state="disabled")
        self.pg_meta.set(meta)

    def ask_pg(self):
        prompt = self.pg_prompt.get("1.0", "end").strip()
        if not prompt:
            self.pg_show("", "type a prompt first")
            return
        self.pg_ask_btn.configure(state="disabled")
        self.pg_pay_btn.configure(state="disabled")
        self.pg_job = None
        self.pg_show("working…", "contacting miners")
        mid = self.mid.get().strip()
        if self.gw:
            threading.Thread(target=self._pg_run, args=(prompt, mid), daemon=True).start()
        else:
            m = self.miner()
            threading.Thread(target=self._pg_run_local,
                             args=(prompt, m.backend, m.model), daemon=True).start()

    def _pg_run(self, prompt, mid):
        try:
            r = self.gw_rpc({"op": "infer_submit", "prompt": prompt, "max_tokens": 128,
                             "customer": mid})
            if not r.get("ok"):
                self.events.put(("pgfail", f"rejected: {r}"))
                return
            jid, quote = r["job_id"], r["quoted_watts"]
            import time as _t
            out, miner = None, ""
            for _ in range(40):
                _t.sleep(3)
                res = self.gw_rpc({"op": "infer_result", "job_id": jid}, timeout=15)
                if res.get("ok"):
                    out, miner = res.get("output", ""), res.get("miner", "")
                    break
            if out is None:
                self.events.put(("pgfail", "no miner served it in time"))
                return
            score = self._pg_score(prompt, out)
            self.pg_job = {"miner": miner, "quote": quote, "output": out}
            meta = f"by {miner[:16]}… quoted {quote} watts" + (f" d1={score:.2f}" if score is not None else "")
            self.events.put(("pg", (out, meta)))
        except (ConnectionError, OSError, ValueError, KeyError) as e:
            self.events.put(("pgfail", f"error: {e}"))

    def _pg_run_local(self, prompt, backend, model):
        try:
            out, lat = backend.generate_raw(prompt, 128)
            score = self._pg_score(prompt, out)
            self.pg_job = {"miner": "local", "quote": 0, "output": out}
            meta = f"local {model} ({lat:.0f}ms)" + (f" d1={score:.2f}" if score is not None else " d1=n/a")
            self.events.put(("pg", (out, meta)))
        except (ConnectionError, OSError, ValueError, KeyError) as e:
            self.events.put(("pgfail", f"error: {e}"))

    @staticmethod
    def _pg_score(prompt, out):
        try:
            from judge import DJudge
            return DJudge(timeout=30).quality(prompt, out)
        except Exception:
            return None

    def pay_pg(self):
        if not self.pg_job or not self.gw:
            self.pg_show(self.pg_answer.get("1.0", "end").strip(),
                         "nothing to pay (local mode is free)")
            return
        to, amount = self.pg_job["miner"], self.pg_job["quote"]
        self.to_id.set(to)
        self.amount.set(str(amount))
        self.send()

    # -- mining loop (worker thread; DB via own connection) ----------------
    def loop(self, miner):
        conn = connect(self.db_path)
        tasks = list(TASKS)
        while self.mining.is_set():
            try:
                ok, s, pay, lat, _ = dispatch(conn, miner, random.choice(tasks), False)
                self.session_jobs += 1
                self.session_earned += pay
                self.events.put(("job", f"job {self.session_jobs}: pass={int(ok)} "
                                 f"+{fmt_watts(pay)} ({lat:.0f}ms)"))
            except Exception as e:
                self.events.put(("job", f"job error: {e}"))
                break
        conn.close()
        self.events.put(("state", "miner stopped."))

    def net_loop(self, cl):
        try:
            while self.mining.is_set():
                try:
                    task, res = cl.run_job()
                except (ConnectionError, OSError, ValueError) as e:
                    self.events.put(("job", f"net error: {e}"))
                    break
                if res.get("ok") is False:
                    self.events.put(("job", f"rejected: {res.get('msg')}"))
                    break
                self.session_jobs += 1
                pay = res.get("payout_watts", 0)
                self.session_earned += pay
                conf = "" if res.get("confirmed", True) else " (unconfirmed)"
                self.events.put(("job", f"job {self.session_jobs}: {task} pass={int(res.get('passed', 0))} "
                                 f"+{fmt_watts(pay)}{conf}"))
        finally:
            try:
                cl.close()
            except Exception:
                pass
            self.events.put(("state", "miner stopped."))

    # -- ui plumbing -------------------------------------------------------
    def say(self, msg, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def kick_fetch(self):
        """Background sync: all gateway RPC off the UI thread. Results land
        in net_cache via events; widgets only ever read the cache."""
        import time as _t
        if self._fetching or _t.time() - self._last_fetch < 3.0:
            return
        self._fetching = True
        mid = self.mid.get().strip()
        def run():
            try:
                bal = int(self.gw_rpc({"op": "balance", "addr": mid}, timeout=12)["balance_watts"])
                try:
                    left = pq.NKEYS - int(self.gw_rpc({"op": "next_ots", "miner_id": mid}, timeout=12)["ots"])
                except (ConnectionError, OSError, ValueError, KeyError):
                    left = None
                try:
                    hist = self.gw_rpc({"op": "history", "addr": mid, "limit": 30}, timeout=12)
                except (ConnectionError, OSError, ValueError, KeyError):
                    hist = {}
                self.events.put(("netdata", {"bal": bal, "left": left,
                                             "jobs": hist.get("jobs", []),
                                             "txs": hist.get("transfers", [])}))
            except (ConnectionError, OSError, ValueError, KeyError):
                self.events.put(("netdata", None))
            finally:
                self._fetching = False
                import time as _t2
                self._last_fetch = _t2.time()
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        if self.gw:
            self.kick_fetch()  # network I/O never runs on the UI thread
        else:
            try:
                import gossip
                n = gossip.collect(self.conn, {self.mid.get().strip(), self.key["addr"]})
                if n:
                    self.say(f"Collected {n} incoming transfer(s).", "good")
            except OSError:
                pass
        try:
            while True:
                kind, msg = self.events.get_nowait()
                if kind == "netdata":
                    if msg is not None:
                        self.net_cache.update(msg)
                        self.status.set(f"{self._mode} — synced")
                    else:
                        self.status.set(f"{self._mode} — unreachable")
                    self.refresh_tabs()
                    self.refresh()
                    continue
                if kind == "pg":
                    out, meta = msg
                    self.pg_show(out, meta)
                    self.pg_ask_btn.configure(state="normal")
                    if self.pg_job and self.gw:
                        self.pg_pay_btn.configure(state="normal")
                    self.refresh()
                    continue
                if kind == "pgfail":
                    self.pg_show("", str(msg))
                    self.pg_ask_btn.configure(state="normal")
                    self.refresh()
                    continue
                self.say(msg, "good" if kind == "job" and "pass=1" in msg
                         else "bad" if "pass=0" in msg or "fail" in msg.lower() or "error" in msg.lower()
                         else None)
                self.refresh_tabs()
                self.refresh()
        except queue.Empty:
            pass
        self.refresh()
        self.root.after(500, self.poll)

    def refresh_tabs(self):
        mid = self.mid.get().strip()
        for tv in (self.jobs_tv, self.tx_tv):
            for i in tv.get_children():
                tv.delete(i)
        if self.gw:
            for jid, passed, pay in sorted(self.net_cache["jobs"], reverse=True)[:30]:
                self.jobs_tv.insert("", "end", values=(f"#{jid}", "pass" if passed else "fail",
                                                      fmt_watts(pay or 0), ""))
            for frm, to, amt in self.net_cache["txs"][:30]:
                d = "in" if to == mid else "out"
                peer = frm if d == "in" else to
                self.tx_tv.insert("", "end", values=(d, (peer or "")[:34], fmt_watts(amt)))
            return
        try:
            for jid, passed, pay in self.conn.execute(
                    "SELECT id, passed, payout_watts FROM jobs WHERE miner_id=? ORDER BY id DESC LIMIT 30",
                    (mid,)):
                self.jobs_tv.insert("", 0, values=(f"#{jid}", "pass" if passed else "fail",
                                                  fmt_watts(pay or 0), ""))
            for frm, to, amt in self.conn.execute(
                    "SELECT from_id, to_id, amount_watts FROM transfers "
                    "WHERE from_id=? OR to_id=? ORDER BY id DESC LIMIT 30", (mid, mid)):
                d = "in" if to == mid else "out"
                peer = frm if d == "in" else to
                self.tx_tv.insert("", 0, values=(d, (peer or "")[:34], fmt_watts(amt)))
        except sqlite3.Error:
            pass

    def refresh(self):
        try:
            if self.gw:
                b, left = self.net_cache["bal"], self.net_cache["left"]
                if b is None:
                    return  # no sync yet; netdata event will fill cache
                self.bal.set(f"{fmt_watts(b)}")
                self.ots_txt.set(f"{left}/{pq.NKEYS} left" if isinstance(left, int) else "sigs: ?")
                if isinstance(left, int):
                    self.ots_bar.configure(value=left)
            else:
                b = balance_watts(self.conn, self.mid.get().strip())
                left = pq.NKEYS - next_ots(self.conn, self.mid.get().strip())
                self.bal.set(f"{fmt_watts(b)}")
                self.ots_bar.configure(value=left)
                self.ots_txt.set(f"{left}/{pq.NKEYS} left")
            self.refresh_tabs()
            if not self.mining.is_set():
                self.status.set(f"idle — session: {self.session_jobs} jobs, +{fmt_watts(self.session_earned)}")
                self.mine_btn.configure(text="▶ Start mining")
                try:
                    self.spin.stop()
                    self.spin.grid_remove()
                except Exception:
                    pass
            else:
                self.status.set(f"mining — session: {self.session_jobs} jobs, +{fmt_watts(self.session_earned)}")
        except sqlite3.Error:
            pass

    def quit(self):
        self.mining.clear()
        self.root.destroy()


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("--profile", default="default")
    _p.add_argument("--gateway", default="", help="host:port of testnet gateway (network mode)")
    _p.add_argument("--pin", default="", help="gateway TLS cert pin")
    _p.add_argument("--no-tls", action="store_true", help="plaintext gateway (loopback tests)")
    _a = _p.parse_args()
    _profile = _a.profile
    _gw = None
    if _a.gateway:
        _h, _pt = _a.gateway.rsplit(":", 1)
        _gw = {"host": _h, "port": int(_pt), "pin": _a.pin, "tls": not _a.no_tls}
        if _gw["tls"] and not _gw["pin"]:
            raise SystemExit("--pin required for TLS gateway")
    import fcntl
    lockf = open(f"/tmp/opencode/infer-wallet-{_profile}.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        import subprocess as _sp
        _sp.run(["notify-send", "Infer Wallet", f"profile '{_profile}' already running"],
                capture_output=True)
        raise SystemExit("wallet profile already running")
    r = tk.Tk()
    Wallet(r, _profile, _gw)
    r.mainloop()
