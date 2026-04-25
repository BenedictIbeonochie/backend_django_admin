"""Slack notifier. No-op when SLACK_BOT_TOKEN is unset."""
import logging
from typing import List

from django.conf import settings

logger = logging.getLogger(__name__)


def post_message(text: str, blocks: List[dict] | None = None, channel: str | None = None) -> bool:
    token = getattr(settings, "SLACK_BOT_TOKEN", "")
    if not token or token.startswith("xoxb-REPLACE"):
        logger.info("SLACK_BOT_TOKEN not configured; skipping Slack post: %s", text[:120])
        return False
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        logger.warning("slack_sdk not installed; cannot post to Slack.")
        return False
    client = WebClient(token=token)
    target = channel or getattr(settings, "SLACK_CHANNEL", "#aqua-admin-alerts")
    try:
        client.chat_postMessage(channel=target, text=text, blocks=blocks)
        return True
    except SlackApiError as exc:  # pragma: no cover - network failures
        logger.exception("Slack post failed: %s", exc.response.get("error") if exc.response else exc)
        return False
