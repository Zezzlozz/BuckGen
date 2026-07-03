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

import logging

from app.config import settings

logger = logging.getLogger("buckgen.llm")

# Maps task_type → settings attr name for the Zen model
_ZEN_MODEL_ATTR: dict[str, str] = {
    "score": "ZEN_MODEL_SCORE",
    "submit": "ZEN_MODEL_SUBMIT",
    "code": "ZEN_MODEL_CODE",
}


async def call_llm(
    task_type: str,
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 256,
    temperature: float = 0.1,
) -> str | None:
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

    # -- 1) OpenCode Zen ----------------------------------------------------
    text = await _call_zen(task_type, prompt, system_prompt, max_tokens, temperature)
    if text is not None:
        return text

    # -- 2) Local Ollama ----------------------------------------------------
    text = await _call_ollama(prompt, max_tokens, temperature)
    if text is not None:
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
) -> str | None:
    """Call OpenCode Zen via its OpenAI-compatible endpoint."""
    if not settings.ZEN_API_KEY:
        logger.debug("ZEN_API_KEY not set — skipping Zen")
        return None

    model = getattr(settings, _ZEN_MODEL_ATTR[task_type], "deepseek-v4-flash-free")
    if not model:
        return None

    try:
        from openai import AsyncOpenAI

        global _zen_client
        if _zen_client is None:
            _zen_client = AsyncOpenAI(
                api_key=settings.ZEN_API_KEY,
                base_url=settings.ZEN_BASE_URL,
                # Use httpx for consistency with the rest of the app (proxy, UA)
                # but don't pass proxy here — let OpenAI's default transport handle it
                # since Zen base URL is hardcoded and doesn't need proxy routing.
            )

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        resp = await _zen_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        text = resp.choices[0].message.content
        if text:
            return text.strip()

        logger.debug("Zen returned empty response")
        return None

    except Exception as exc:
        logger.debug("Zen call failed: %s", exc)
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
            timeout=60.0,
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
