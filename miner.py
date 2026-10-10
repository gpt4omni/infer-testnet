#!/usr/bin/env python3
"""Miner clients. MockBackend runs today; LlamaCppBackend hits a local
llama-server OpenAI-compatible endpoint (/v1/chat/completions) for the
1080 Ti step. Same interface so eval/gateway don't care."""
import hashlib
import os
import time
import json
import urllib.request


class MockBackend:
    """Two personalities: 'good' writes correct code, 'cheat' returns garbage fast."""
    def __init__(self, personality="good"):
        assert personality in ("good", "cheat")
        self.personality = personality

    def generate(self, prompt: str) -> str:
        t0 = time.time()
        if self.personality == "good":
            time.sleep(0.05)
            out = GOOD_FIXTURES.get(prompt, "def solve(x):\n    return x\n")
        else:
            time.sleep(0.005)  # cheat is fast but wrong
            out = "def solve(x):\n    return 42  # cheap garbage\n"
        return out, (time.time() - t0) * 1000

    def generate_raw(self, prompt: str, max_tokens: int = 256) -> tuple[str, float]:
        t0 = time.time()
        time.sleep(0.02 if self.personality == "good" else 0.002)
        if self.personality == "good":
            out = f"mock answer ({len(prompt)} chars in): {prompt[:200]}"
        else:
            out = "garbage garbage garbage"
        return out, (time.time() - t0) * 1000


GOOD_FIXTURES = {
    "add": "def solve(a, b):\n    return a + b\n",
    "fib": "def solve(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "reverse": "def solve(s):\n    return s[::-1]\n",
    "sortnums": "def solve(xs):\n    return sorted(xs)\n",
    "vowels": "def solve(s):\n    return sum(1 for c in s if c in \"aeiouAEIOU\")\n",
}


TASK_STUBS = {
    "add": 'def solve(a, b):\n    """Return a+b."""',
    "fib": 'def solve(n):\n    """Return the nth Fibonacci number (solve(0)=0, solve(1)=1)."""',
    "reverse": 'def solve(s):\n    """Return s reversed."""',
    "sortnums": 'def solve(xs):\n    """Return sorted(xs) ascending."""',
    "vowels": 'def solve(s):\n    """Return the number of vowels (aeiouAEIOU) in s."""',
}


class LlamaCppBackend:
    def __init__(self, url="http://127.0.0.1:19091/v1/chat/completions",
                 model="qwen2.5-0.5b-Q4", api_key=None, timeout=180):
        self.url, self.model, self.timeout = url, model, timeout
        if api_key is None:  # atomic-chat default key location
            try:
                with open(os.path.expanduser("~/.atomic-agent/models/llama-server.key")) as f:
                    api_key = f.read().strip()
            except OSError:
                api_key = None
        self.api_key = api_key

    def generate(self, prompt: str) -> str:
        t0 = time.time()
        stub = TASK_STUBS.get(prompt, "def solve(x):\n    pass")
        user = ("Define EXACTLY this Python function and nothing else "
                "(no other functions, no print, no examples). "
                "Output only a python fenced code block.\n\n" + stub)
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.0, "max_tokens": 1024}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"}
                                                 if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            msg = json.load(r)["choices"][0]["message"]
        txt = msg.get("content") or ""
        if "def solve" not in txt and msg.get("reasoning_content"):
            txt = msg["reasoning_content"]  # reasoning model: answer stuck in thinking trace
        code = extract_code(txt)
        return code, (time.time() - t0) * 1000

    def generate_raw(self, prompt: str, max_tokens: int = 256) -> tuple[str, float]:
        """Open-ended inference: prompt goes through untouched (no code framing)."""
        t0 = time.time()
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7, "max_tokens": max(16, min(max_tokens, 1024))}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"}
                                                 if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            msg = json.load(r)["choices"][0]["message"]
        txt = (msg.get("content") or "") or (msg.get("reasoning_content") or "")
        return txt.strip()[:8000], (time.time() - t0) * 1000


def extract_code(txt: str) -> str:
    if "```" in txt:
        parts = txt.split("```")
        for p in parts:
            s = p.strip()
            if s.startswith("python"):
                s = s[6:].strip()
            if "def solve" in s:
                return s
    return txt.strip()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]
