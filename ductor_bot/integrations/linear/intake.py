"""Turn raw brainstorm text into a structured LinearIssueDraft."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

import aiohttp

from ductor_bot.integrations.linear.models import LinearIssueDraft

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a task structuring assistant.
The user sends raw brainstorm/idea text in Russian (or mixed languages).
Turn it into a structured dev task.

Return ONLY valid JSON:
{
  "title": "concise task title, max 80 chars, Russian",
  "description": "## Контекст\\n...\\n## Что сделать\\n...\\n## Ожидаемый результат\\n...",
  "acceptance": "- criterion 1\\n- criterion 2",
  "priority": 3
}

Rules:
- Keep user's intent exactly, don't add scope
- If user mentions specific tools/services, include them
- priority: 0=none, 1=urgent, 2=high, 3=medium, 4=low
- Write in Russian"""


_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _as_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"{field} is not an object"
        raise TypeError(msg)
    return value


def _as_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        msg = f"{field} is not a string"
        raise TypeError(msg)
    return value


def _extract_json_text(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < 0 or end <= start:
        msg = "AI response does not contain JSON object"
        raise ValueError(msg)
    return stripped[start : end + 1]


async def structure_task(
    raw_text: str,
    *,
    provider: str = "openai",
    model: str = "gpt-4.1-mini",
    api_key: str = "",
) -> LinearIssueDraft:
    if provider == "passthrough":
        return LinearIssueDraft(title=raw_text[:80], description=raw_text)
    if provider == "openai":
        return await _call_openai(raw_text, model, api_key)
    if provider == "anthropic":
        return await _call_anthropic(raw_text, model, api_key)
    msg = f"Unknown intake provider: {provider}"
    raise ValueError(msg)


async def _call_openai(text: str, model: str, api_key: str) -> LinearIssueDraft:
    if not api_key.strip():
        msg = "OpenAI API key is empty"
        raise ValueError(msg)

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    async with (
        aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT) as session,
        session.post(url, json=payload, headers=headers) as response,
    ):
        body = await response.json(content_type=None)
        if response.status >= 400:
            msg = f"OpenAI HTTP {response.status}: {body}"
            raise RuntimeError(msg)

    data = _as_mapping(body, field="openai.response")
    choices_obj = data.get("choices")
    if not isinstance(choices_obj, list) or not choices_obj:
        msg = "openai.response.choices is empty"
        raise ValueError(msg)
    first_choice = _as_mapping(choices_obj[0], field="openai.response.choices[0]")
    message = _as_mapping(first_choice.get("message"), field="openai.response.choices[0].message")
    content = _as_str(message.get("content"), field="openai.response.choices[0].message.content")
    return LinearIssueDraft(**json.loads(_extract_json_text(content)))


async def _call_anthropic(text: str, model: str, api_key: str) -> LinearIssueDraft:
    if not api_key.strip():
        msg = "Anthropic API key is empty"
        raise ValueError(msg)

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload: dict[str, object] = {
        "model": model,
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}],
    }

    async with (
        aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT) as session,
        session.post(url, json=payload, headers=headers) as response,
    ):
        body = await response.json(content_type=None)
        if response.status >= 400:
            msg = f"Anthropic HTTP {response.status}: {body}"
            raise RuntimeError(msg)

    data = _as_mapping(body, field="anthropic.response")
    content_obj = data.get("content")
    if not isinstance(content_obj, list) or not content_obj:
        msg = "anthropic.response.content is empty"
        raise ValueError(msg)
    first_block = _as_mapping(content_obj[0], field="anthropic.response.content[0]")
    content = _as_str(first_block.get("text"), field="anthropic.response.content[0].text")
    json_text = _extract_json_text(content)
    return LinearIssueDraft(**json.loads(json_text))
