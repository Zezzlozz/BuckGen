"""
Unified LLM client with automatic model selection per task.

Priority chain:
  1. OpenCode Zen (OpenAI-compatible API, works from anywhere)
  2. Local Ollama (fallback, only works when running locally)
  3. Returns None — caller falls back to heuristic / template

Task types and their preferred models:
  - "score"  → deepseek-v4-flash-free / qwen2.5-coder:7b
  - "submit" → deepseek-v4-flash-free / qwen2.5-coder:7b
  - "code"   → north-mini-code-free  / qwen2.5-coder:7b
"""

import asyncio
import hashlib
import logging
import random
import time

from app.config import settings

logger = logging.getLogger("buckgen.llm")

# In-memory TTL cache for LLM responses to avoid redundant API calls.
# Keyed by hash(task_type + prompt), valued by (timestamp, response).
# Cached items expire after settings.LLM_CACHE_TTL seconds.
LLM_CACHE_TTL = settings.LLM_CACHE_TTL
_llm_cache: dict[str, tuple[float, str]] = {}


def _cache_key(task_type: str, prompt: str) -> str:
    """Generate a deterministic cache key for an LLM call."""
    raw = f"{task_type}||{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> str | None:
    """Get cached response if not expired."""
    entry = _llm_cache.get(key)
    if entry is None:
        return None
    timestamp, text = entry
    if time.time() - timestamp > LLM_CACHE_TTL:
        del _llm_cache[key]
        return None
    return text


def _cache_set(key: str, text: str) -> None:
    """Store a response in the cache."""
    _llm_cache[key] = (time.time(), text)


# Maps task_type → settings attr name for the primary Zen model
_ZEN_MODEL_ATTR: dict[str, str] = {
    "score": "ZEN_MODEL_SCORE",
    "submit": "ZEN_MODEL_SUBMIT",
    "code": "ZEN_MODEL_CODE",
}

# Fallback chains per task (tried in order if primary fails on non-auth error)
_ZEN_FALLBACK_MODELS: dict[str, list[str]] = {
    "score": ["mimo-v2.5-free", "north-mini-code-free"],
    "submit": ["mimo-v2.5-free", "north-mini-code-free"],
    "code": ["deepseek-v4-flash-free", "mimo-v2.5-free"],
}


async def call_llm(
    task_type: str,
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 256,
    temperature: float | None = None,
) -> str | None:
    if temperature is None:
        temperature = settings.LLM_TEMPERATURE

    """
    Call an LLM with automatic model selection per task type.

    Tries OpenCode Zen first, then local Ollama, returns ``None`` if
    both are unavailable.

    Args:
        task_type: One of ``"score"``, ``"submit"``, ``"code"``.
        prompt: The user prompt to send.
        system_prompt: Optional system message.
        max_tokens: Maximum tokens in the response.
        temperature: LLM temperature (0.0 = deterministic).

    Returns:
        Response text, or ``None`` on failure.
    """
    if task_type not in _ZEN_MODEL_ATTR:
        logger.warning("Unknown task_type %r — defaulting to 'code'", task_type)
        task_type = "code"

    # -- 0) Check cache -----------------------------------------------------
    key = _cache_key(task_type, prompt)
    cached = _cache_get(key)
    if cached is not None:
        logger.debug("LLM cache hit for %s (%d chars)", task_type, len(prompt))
        return cached

    # -- 1) OpenCode Zen ----------------------------------------------------
    text = await _call_zen(task_type, prompt, system_prompt, max_tokens, temperature)
    if text is not None:
        _cache_set(key, text)
        return text

    # -- 2) Local Ollama ----------------------------------------------------
    text = await _call_ollama(prompt, max_tokens, temperature)
    if text is not None:
        _cache_set(key, text)
        return text

    return None


# ---------------------------------------------------------------------------
# OpenCode Zen (OpenAI-compatible API)
# ---------------------------------------------------------------------------

_zen_client = None


async def _call_zen(
    task_type: str,
    prompt: str,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    retries: int = 2,
) -> str | None:
    """
    Call OpenCode Zen via its OpenAI-compatible endpoint with retry logic.

    Retries on transient errors (rate limits, 5xx, network timeouts).
    Non-retryable errors (400, 401, 403) fail immediately.

    Args:
        task_type: One of ``"score"``, ``"submit"``, ``"code"``.
        prompt: The user prompt.
        system_prompt: Optional system message.
        max_tokens: Max tokens in response.
        temperature: LLM temperature (0.0 = deterministic).
        retries: Number of retries on transient errors (default 2).

    Returns:
        Response text, or ``None`` on failure.
    """
    if not settings.ZEN_API_KEY:
        logger.debug("ZEN_API_KEY not set — skipping Zen")
        return None

    primary = getattr(settings, _ZEN_MODEL_ATTR[task_type], "deepseek-v4-flash-free")
    if not primary:
        return None

    fallbacks = _ZEN_FALLBACK_MODELS.get(task_type, [])
    models_to_try = [primary] + fallbacks

    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AsyncOpenAI,
        RateLimitError,
    )

    global _zen_client
    if _zen_client is None:
        _zen_client = AsyncOpenAI(
            api_key=settings.ZEN_API_KEY,
            base_url=settings.ZEN_BASE_URL,
            # Proxy is handled by httpx's default transport — Zen base URL
            # doesn't need explicit proxy routing.
        )

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for model in models_to_try:
        for attempt in range(retries + 1):
            try:
                resp = await _zen_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

                text = resp.choices[0].message.content
                if text:
                    return text.strip()

                logger.debug("Zen returned empty response for %s", model)
                break  # empty response is unlikely to succeed with different model

            except RateLimitError:
                logger.debug(
                    "Zen %s rate limited (attempt %d/%d)",
                    model,
                    attempt + 1,
                    retries + 1,
                )
                if attempt < retries:
                    wait = (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(wait)
                    continue
                logger.debug(
                    "Zen %s rate limited after %d retries — trying fallback",
                    model,
                    retries + 1,
                )

            except (APITimeoutError, APIConnectionError):
                logger.debug(
                    "Zen %s network error (attempt %d/%d)",
                    model,
                    attempt + 1,
                    retries + 1,
                )
                if attempt < retries:
                    wait = (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(wait)
                    continue
                logger.debug(
                    "Zen %s unreachable after %d retries — trying fallback",
                    model,
                    retries + 1,
                )

            except APIStatusError as exc:
                # 4xx auth errors are fatal — don't try other models
                if exc.status_code in (400, 401, 403):
                    logger.debug(
                        "Zen auth error (HTTP %d) — not retrying", exc.status_code
                    )
                    return None
                if exc.status_code >= 500 and attempt < retries:
                    logger.debug(
                        "Zen %s HTTP %d (attempt %d/%d)",
                        model,
                        exc.status_code,
                        attempt + 1,
                        retries + 1,
                    )
                    wait = (2**attempt) + random.uniform(0, 1)
                    await asyncio.sleep(wait)
                    continue
                logger.debug(
                    "Zen %s non-retryable HTTP %d — trying fallback",
                    model,
                    exc.status_code,
                )

            except Exception as exc:
                logger.debug("Zen %s unexpected error: %s", model, exc)

            # If we get here, retries are exhausted — try next model
            break

    return None


# ---------------------------------------------------------------------------
# Local Ollama fallback
# ---------------------------------------------------------------------------


async def _call_ollama(
    prompt: str,
    max_tokens: int,
    temperature: float,
    model: str | None = None,
) -> str | None:
    """Call a local Ollama instance."""
    import httpx

    payload = {
        "model": model or settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }

    try:
        async with httpx.AsyncClient(
            timeout=settings.LLM_TIMEOUT,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            resp = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()
            return text or None

    except Exception as exc:
        logger.debug("Ollama unavailable at %s: %s", settings.OLLAMA_BASE_URL, exc)
        return None
