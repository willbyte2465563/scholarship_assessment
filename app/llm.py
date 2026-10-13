"""The model behind a small interface, so scoring can be tested without the network."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace
from typing import Protocol


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    failure: str | None = None  # set when the call errored, was blocked or didn't finish
    model_version: str | None = None  # what the provider says actually ran
    usage: dict | None = None  # token counts: input, output, thinking
    backoff_retries: int = 0  # rate-limit or transient retries before this reply


class LLM(Protocol):
    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply: ...


# Failures worth waiting out: rate limits, server errors, network trouble. Anything else
# (a block, a bad request) comes straight back and is handled as a scoring attempt.
TRANSIENT = ("api_error: 429", "api_error: 500", "api_error: 502", "api_error: 503", "api_error: 504", "call_failed")


class Backoff:
    """Retries transient failures with exponential backoff and jitter, below the scoring retry
    loop, so a rate limit never uses up a validation attempt or holds an applicant."""

    def __init__(self, inner: LLM, max_tries: int = 6, base_seconds: float = 2.0, cap_seconds: float = 60.0,
                 sleep=time.sleep, jitter=random.random):
        self.inner, self.max_tries, self.base, self.cap = inner, max_tries, base_seconds, cap_seconds
        self.sleep, self.jitter = sleep, jitter

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        for n in range(self.max_tries):
            reply = self.inner.generate(prompt, model=model, temperature=temperature)
            if not (reply.failure or "").startswith(TRANSIENT) or n == self.max_tries - 1:
                return replace(reply, backoff_retries=n)
            self.sleep(min(self.cap, self.base * 2 ** n) * (0.5 + self.jitter() / 2))
        raise AssertionError("unreachable")


class GeminiClient:
    """Calls Gemini in JSON mode. Never raises: every failure comes back as a reply with `failure` set."""

    def __init__(self, api_key: str):
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply:
        from google.genai import errors, types

        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json"),
            )
        except errors.APIError as e:
            return LLMReply(None, f"api_error: {e.code} {e.status}")
        except Exception as e:  # network and anything else: the applicant is held, not dropped
            return LLMReply(None, f"call_failed: {type(e).__name__}")

        version = response.model_version
        meta = response.usage_metadata
        usage = meta and {
            "input": meta.prompt_token_count,
            "output": meta.candidates_token_count,
            "thinking": meta.thoughts_token_count,
        }
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            return LLMReply(None, f"blocked: {feedback.block_reason}", version, usage)
        if not response.candidates:
            return LLMReply(None, "no_candidates", version, usage)
        finish = response.candidates[0].finish_reason
        if finish not in (None, types.FinishReason.STOP):
            return LLMReply(response.text, f"finish_reason: {finish}", version, usage)
        return LLMReply(response.text, None, version, usage)
