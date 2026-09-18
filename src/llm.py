"""Provider-agnostic LLM client (text generation).

Exposes one function, `complete(system, user, max_tokens)`, driven entirely by
config: base URL, key, model. Swapping provider later (gpt-5-mini, an open-weight
host) is a config change, not a code change — it is an OpenAI-compatible client.

Per call:
  * max_tokens set explicitly from config.
  * thinking mode off. DeepSeek: off by default; we also send it explicitly via
    extra_body={"thinking": {"type": "disabled"}} (verified against
    api-docs.deepseek.com). Non-DeepSeek providers ignore extra_body.
  * bounded retries: <=3, only on 429/5xx/timeouts, capped exponential backoff.
    Never retry a 4xx. No retry logic nested inside another retrying loop.
  * static system prompt + master CV go first so DeepSeek prefix-caching applies.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# token accounting for the run (logged to stdout by main)
_USAGE = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}


def usage() -> dict:
    return dict(_USAGE)


def reset_usage() -> None:
    for k in _USAGE:
        _USAGE[k] = 0


def make_client(cfg):
    """Build the OpenAI-compatible client from config + secrets."""
    from openai import OpenAI

    llm = cfg.llm
    key = cfg.secrets.deepseek_api_key
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set.")
    return OpenAI(api_key=key, base_url=llm.get("base_url", "https://api.deepseek.com"))


def _thinking_extra_body(cfg) -> dict:
    """extra_body to disable thinking for DeepSeek. Empty for providers that
    don't understand it (harmless — OpenAI SDK forwards nothing)."""
    if str(cfg.llm.get("provider", "")).lower() == "deepseek" and not cfg.llm.get("thinking", False):
        return {"thinking": {"type": "disabled"}}
    return {}


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def complete(system: str, user: str, max_tokens: int, *, client=None, cfg=None) -> str:
    """One completion. `client` and `cfg` are passed by the caller so the client
    is built once per run. Retries transient failures only, up to 3 times."""
    if client is None or cfg is None:
        raise ValueError("complete() requires client and cfg (build once per run).")

    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        RateLimitError,
    )

    model = cfg.llm.get("model", "deepseek-flash")
    extra_body = _thinking_extra_body(cfg)
    # System prompt first => stable prefix for DeepSeek automatic prefix caching.
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.4,
                extra_body=extra_body or None,
            )
            _account(resp)
            return (resp.choices[0].message.content or "").strip()
        except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
        except APIStatusError as exc:
            if exc.status_code in _RETRYABLE_STATUS:
                last_exc = exc
            else:
                raise  # never retry a 4xx
        if attempt < 2:
            log.warning("LLM transient failure (attempt %d): %s — backing off %.1fs",
                        attempt + 1, last_exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    raise RuntimeError(f"LLM call failed after retries: {last_exc}")


def _account(resp) -> None:
    _USAGE["calls"] += 1
    u = getattr(resp, "usage", None)
    if not u:
        return
    _USAGE["prompt"] += getattr(u, "prompt_tokens", 0) or 0
    _USAGE["completion"] += getattr(u, "completion_tokens", 0) or 0
    _USAGE["total"] += getattr(u, "total_tokens", 0) or 0
