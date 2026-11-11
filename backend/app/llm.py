"""Minimal LLM client: OpenAI-compatible chat completions (OpenAI, Ollama, LM Studio, OpenRouter, vLLM, ...)
or the Anthropic Messages API."""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("pkdb.llm")


class LLMError(Exception):
    pass


async def chat(system: str, user: str, json_output: bool = True) -> str:
    if settings.llm_provider == "anthropic":
        return await _anthropic(system, user)
    return await _openai(system, user, json_output)


async def _openai(system: str, user: str, json_output: bool) -> str:
    base = settings.llm_base_url or "https://api.openai.com/v1"
    body: dict = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    if json_output and settings.llm_json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    async with httpx.AsyncClient(timeout=settings.llm_timeout) as c:
        r = await c.post(f"{base}/chat/completions", json=body, headers=headers)
        if r.status_code == 400 and "response_format" in body:
            # Some servers do not support JSON mode; retry without it
            body.pop("response_format")
            r = await c.post(f"{base}/chat/completions", json=body, headers=headers)
    if r.status_code >= 400:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected LLM response: {str(data)[:300]}") from e


async def _anthropic(system: str, user: str) -> str:
    base = settings.llm_base_url or "https://api.anthropic.com"
    body = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": settings.llm_temperature,
    }
    headers = {
        "x-api-key": settings.llm_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout) as c:
        r = await c.post(f"{base}/v1/messages", json=body, headers=headers)
    if r.status_code >= 400:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json(text: str) -> dict:
    """Extract a JSON object from an LLM answer (tolerates code fences and surrounding prose)."""
    t = text.strip()
    t = re.sub(r"^<think>.*?</think>", "", t, flags=re.S).strip()  # reasoning models (e.g. via Ollama)
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m and "{" in m.group(1):
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(t[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM returned invalid JSON: {e}") from e
    raise LLMError("LLM answer did not contain a JSON object")


async def chat_json(system: str, user: str, retries: int = 1) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        text = await chat(system, user, json_output=True)
        try:
            return parse_json(text)
        except LLMError as e:
            last = e
            log.warning("Invalid JSON from LLM (attempt %d): %s", attempt + 1, e)
            user = user + "\n\nIMPORTANT: Answer with ONE valid JSON object only, no other text."
    raise last or LLMError("LLM failed")
