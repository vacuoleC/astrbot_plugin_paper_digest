"""LLM 调用公共工具：provider 解析、文本调用、JSON 提取。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def resolve_provider(context, provider_id: str | None):
    """按配置的 provider id 取 provider；为空或找不到时回退默认 provider。"""
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id)
            if provider:
                return provider
            logger.warning("provider %s not found, falling back to default", provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_provider_by_id(%s) failed: %s", provider_id, exc)
    try:
        return context.get_using_provider()
    except Exception as exc:  # noqa: BLE001
        logger.error("get_using_provider failed: %s", exc)
        return None


def provider_name(provider) -> str:
    if provider is None:
        return "none"
    cfg = getattr(provider, "provider_config", None) or {}
    return str(cfg.get("id") or cfg.get("type") or type(provider).__name__)


async def chat_text(provider, *, system_prompt: str | None, user_prompt: str,
                    max_retries: int = 2) -> str:
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = getattr(response, "completion_text", None)
            if text is None:
                text = str(response)
            return text
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("LLM call failed (%s/%s): %s", attempt + 1, max_retries, exc)
    raise RuntimeError(f"LLM call failed: {last}")


def extract_json(text: str) -> Any:
    """从模型输出中提取第一个合法 JSON 对象/数组，容忍 markdown 代码块。"""
    if text is None:
        raise ValueError("empty LLM output")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"[\[{]", cleaned)]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("no valid JSON found in LLM output")
