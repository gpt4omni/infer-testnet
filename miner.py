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


GOOD_FIXTURES = {
    "add": "def solve(a, b):\n    return a + b\n",
    "fib": "def solve(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "reverse": "def solve(s):\n    return s[::-1]\n",
    "sortnums": "def solve(xs):\n    return sorted(xs)\n",
    "vowels": "def solve(s):\n    return sum(1 for c in s if c in \"aeiouAEIOU\")\n",
}


class LlamaCppBackend:
    def __init__(self, url="http://127.0.0.1:19091/v1/chat/completions",
                 model="gemma-4-12b", api_key=None, timeout=180):
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
        user = ("Output ONLY a python fenced code block defining solve(). "
                "No explanation, no analysis. "
                "Signatures: add->solve(a,b), fib->solve(n), reverse->solve(s), "
                "sortnums->solve(xs) returns sorted(xs), vowels->solve(s) returns int vowel count. "
                f"Task: {prompt}")
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
