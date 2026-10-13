"""A scripted stand-in for the model, so scoring tests never touch the network."""

import json
import re
import threading
from collections import defaultdict

from app.intake import CRITERIA
from app.llm import LLMReply


class FakeLLM:
    def __init__(self, *replies):
        """Each reply is an LLMReply, or a str/dict used as the reply text."""
        self.replies = list(replies)
        self.calls = []

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        self.calls.append({"prompt": prompt, "model": model, "temperature": temperature})
        reply = self.replies.pop(0)
        if isinstance(reply, LLMReply):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMReply(text, None, "fake-model-001")


def good_reply(evidence=("gpa",), scores=(7, 8, 5, 6, 9), confidence="medium") -> dict:
    return {
        "scores": [
            {"criterion": c, "score": s, "confidence": confidence, "evidence": list(evidence),
             "rationale": "Plain reason."}
            for c, s in zip(CRITERIA, scores)
        ]
    }


class VaryingLLM:
    """Thread-safe. Reads the applicant's seed from their essay (tests.conftest.make_text) and
    scores every criterion seed % 5 + 3. Each applicant's runs score base, base+1, base in call
    order, so the median is base and the spread is 1 whatever order the workers finish in."""

    def __init__(self, raise_for: int | None = None, block_after_first: int | None = None):
        self.raise_for, self.block_after_first = raise_for, block_after_first
        self.lock, self.seen, self.calls = threading.Lock(), defaultdict(int), 0

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        seed = int(re.search(r"word(\d+)x0", prompt).group(1))
        with self.lock:
            n = self.seen[seed]
            self.seen[seed] += 1
            self.calls += 1
        if seed == self.raise_for:
            raise RuntimeError("boom")
        if seed == self.block_after_first and n >= 1:
            return LLMReply(None, "blocked: SAFETY")
        base = seed % 5 + 3
        reply = good_reply(scores=(base + (n % 3 == 1),) * len(CRITERIA), evidence=(f"word{seed}x0 word{seed}x1", "gpa"))
        for item in reply["scores"]:
            item["rationale"] = f"Run {n + 1} reasoning for {item['criterion']}."
        return LLMReply(json.dumps(reply), None, "fake")


class Boom:
    def generate(self, *a, **k):
        raise AssertionError("the model was called")
