"""Cost estimates for model calls, from config/pricing.json. Shared by batch scoring and the evals."""

from __future__ import annotations

import json
import math
from pathlib import Path

PRICING = Path("config/pricing.json")


def load_pricing(path: Path = PRICING) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def estimate(prompt_chars: int, calls: int, cfg: dict) -> dict:
    """cfg holds "pricing" and "estimate". Thinking tokens bill as output."""
    est, price = cfg["estimate"], cfg["pricing"]
    margin = 1 + est["retry_margin"]
    tokens_in = prompt_chars / est["chars_per_token"]
    tokens_out = calls * (est["output_tokens_per_call"] + est["thinking_tokens_per_call"])
    usd = (tokens_in * price["input_usd_per_million"] + tokens_out * price["output_usd_per_million"]) / 1e6
    return {"calls": calls, "calls_with_retries": math.ceil(calls * margin), "input_tokens": round(tokens_in),
            "output_tokens": round(tokens_out), "usd": round(usd, 2), "usd_with_retries": round(usd * margin, 2)}


def describe(est: dict, cfg: dict, model: str) -> list[str]:
    price, e = cfg["pricing"], cfg["estimate"]
    lines = [
        f"Estimated tokens: ~{est['input_tokens']:,} input, ~{est['output_tokens']:,} output including "
        f"~{e['thinking_tokens_per_call']:,} thinking tokens per call ({e['measured_from']})",
        f"Estimated cost: ${est['usd']:.2f} (up to ${est['usd_with_retries']:.2f} with {e['retry_margin']:.0%} retries)",
        f"Price: ${price['input_usd_per_million']}/M input, ${price['output_usd_per_million']}/M output for "
        f"{price['model']}. {price['note']}",
        f"Source: {price['source']} (checked {price['checked']})",
    ]
    if price["model"] != model:
        lines.append(f"WARNING: the price is for {price['model']} but scoring uses {model}")
    return lines
