#!/usr/bin/env python3
"""d1 quality judge: Liquid AI decision model as LLM-as-judge replacement.

Why d1 instead of asking another LLM "is this good?":
- Calibrated probabilities, not vibes: Noul returns P(correct), stable
  across runs (no sampling temperature games).
- Single forward pass, milliseconds, zero output tokens: every gateway
  and evaluator can run d1-omni-600M on CPU. Judging stays cheaper than
  serving, which is the whole economic point.
- No generated text = tiny prompt-injection surface vs judge-LLMs.

Role in Infer: objective tasks (code unit tests) still rule absolutely.
d1 scores SUBJECTIVE/open-ended inference + provides a second opinion
that feeds miner capability profiles and gateway routing weight.
A d1 score never overrules ground truth; it prices what truth can't check.
"""
import json
import urllib.request

JUDGE_URL = "http://127.0.0.1:19092/v1/systemone"

# Hosted fallback (Jev API, System-One compatible). Key NEVER in source:
# export JEV_API_KEY=...  / Railway service variable.
JEV_URL = "https://jev-api.com/v1/systemone"
JEV_MODEL = "jev-latest"


def _ask_remote(state: str, questions: dict, timeout: float = 60.0) -> dict | None:
    import os as _os
    key = _os.environ.get("JEV_API_KEY", "")
    if not key:
        return None
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(JEV_URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _ask(url: str, state: str, questions: dict, timeout: float = 60.0) -> dict | None:
    body = json.dumps({"state": state, "questions": questions}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None  # judge down = abstain; callers fall back (never fail-open into pay)


def _prob(ans, key="probability", alt=("p", "score", "value")) -> float | None:
    if isinstance(ans, (int, float)):
        return max(0.0, min(1.0, float(ans)))
    if isinstance(ans, dict):
        for k in (key,) + tuple(alt):
            if k in ans:
                return _prob(ans[k])
    return None


def _noul(ans) -> float | None:
    if isinstance(ans, (int, float)):
        return max(0.0, min(1.0, float(ans)))
    if isinstance(ans, dict):
        for k in ("noul", "probability", "p"):
            if k in ans:
                return _noul(ans[k])
    return None


def _score_norm(ans) -> float | None:
    """Score-type answer -> [0,1] via legend range (d1 /v1/systemone shape)."""
    if isinstance(ans, dict) and "score" in ans and "legend" in ans:
        try:
            lo = min(int(k) for k in ans["legend"])
            hi = max(int(k) for k in ans["legend"])
            if hi > lo:
                return max(0.0, min(1.0, (float(ans["score"]) - lo) / (hi - lo)))
        except (ValueError, TypeError):
            pass
    return _prob(ans)


class DJudge:
    """Calibrated answer-quality rater over a d1 endpoint."""

    def __init__(self, url: str = JUDGE_URL, timeout: float = 60.0):
        self.url, self.timeout = url, timeout

    def rate(self, prompt: str, answer: str, reference: str = "") -> dict:
        """Returns {correct_p, useful_p} in [0,1] (None each if abstaining).
        Local d1 first; hosted Jev API fallback (needs JEV_API_KEY)."""
        ref = f"\nReference (ground truth, may be empty): {reference}" if reference else ""
        state = f"Task: {prompt}\nMiner answer: {answer[:2000]}{ref}"
        questions = {
            "correct": {"type": "noul", "instructions": "Is the miner answer correct for the task?"},
            "useful": {"type": "score", "instructions": "How useful is the miner answer?",
                       "criteria": ["useless", "partially useful", "fully useful"]},
        }
        out = _ask(self.url, state, questions, self.timeout)
        if out is None:
            out = _ask_remote(state, questions, self.timeout)
        if not out:
            return {"correct_p": None, "useful_p": None}
        res = out.get("answers", out.get("results", out))
        cp = _noul(res.get("correct"))
        up = _score_norm(res.get("useful"))
        return {"correct_p": cp, "useful_p": up}

    def quality(self, prompt: str, answer: str, reference: str = "") -> float | None:
        """Single number for pricing: mean of available signals, None if judge down."""
        r = self.rate(prompt, answer, reference)
        ps = [x for x in (r["correct_p"], r["useful_p"]) if x is not None]
        return sum(ps) / len(ps) if ps else None
