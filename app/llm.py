"""The model behind a small interface, so scoring can be tested without the network."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMReply:
    text: str | None
    failure: str | None = None  # set when the call errored, was blocked or didn't finish
    model_version: str | None = None  # what the provider says actually ran
    usage: dict | None = None  # token counts: input, output, thinking


class LLM(Protocol):
    def generate(self, prompt: str, *, model: str, temperature: float) -> LLMReply: ...


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
