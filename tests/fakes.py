"""A scripted stand-in for the model, so scoring tests never touch the network."""

import json

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
