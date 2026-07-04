"""
Telegram notification utility.
Sends alerts when high-value bounties are found or errors occur.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("buckgen.notify")


async def send_telegram(message: str, parse_mode: str = "Markdown") -> bool:
    """Send a Telegram message.  Returns True on success."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return True
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Telegram send failed (HTTP %d): %s",
            exc.response.status_code,
            exc.response.text[:120],
        )
        return False
    except httpx.RequestError as exc:
        logger.warning("Telegram send failed (network): %s", exc)
        return False


async def notify_bounty_found(bounty: dict[str, Any]) -> None:
    """Alert on a high-scoring bounty."""
    title = bounty.get("title", "Untitled")
    reward = bounty.get("reward_amount", 0)
    currency = bounty.get("reward_currency", "USD")
    url = bounty.get("url", "")
    score = bounty.get("score", 0)

    msg = (
        f"💰 *High-Value Bounty Found*\n"
        f"Score: `{score:.2f}`\n"
        f"*{title}*\n"
        f"Reward: {reward} {currency}\n"
        f"[View]({url})"
        if url
        else ""
    )
    await send_telegram(msg)


async def notify_error(context: str, detail: str) -> None:
    """Alert on a module error."""
    msg = f"[WARN] *BuckGen Error*\n`{context}`\n{detail[:200]}"
    await send_telegram(msg)


async def notify_alert(title: str, body: str = "") -> None:
    """Send a general alert to Telegram."""
    msg = f"[ALERT] *{title}*\n{body}" if body else f"[ALERT] *{title}*"
    await send_telegram(msg)
