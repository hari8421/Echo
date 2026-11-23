"""Provider-neutral chat client with tool-call normalization."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import List, Optional

from .config import Config

TIMEOUT = 300


class LLMError(Exception):
    pass


def _post(url: str, payload: dict, headers: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise LLMError("HTTP %s from %s: %s" % (exc.code, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise LLMError(_connection_hint(url, exc.reason)) from exc
    except TimeoutError as exc:
        raise LLMError("Request to %s timed out. Try a smaller model or a shorter task." % url) from exc
    except json.JSONDecodeError as exc:
        raise LLMError("The model server returned a non-JSON response.") from exc


def _get(url: str, headers: Optional[dict] = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise LLMError("HTTP %s from %s: %s" % (exc.code, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise LLMError(_connection_hint(url, exc.reason)) from exc


def _connection_hint(url: str, reason) -> str:
    if "11434" in url or "localhost" in url or "127.0.0.1" in url:
        return (
            "Cannot reach the local model server at %s (%s).\n"
            "Start Ollama with `ollama serve`, then `ollama pull llama3.2` "
            "(or set ECHO_MODEL to a model you already have)." % (url, reason)
        )
    return "Cannot reach %s (%s). Check ECHO_BASE_URL, your network and ECHO_API_KEY." % (url, reason)


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> dict:
        if self.cfg.provider == "ollama":
            return self._ollama_chat(messages, tools)
        return self._openai_chat(messages, tools)

    # -- Ollama ------------------------------------------------------------
    def _ollama_chat(self, messages, tools) -> dict:
        payload = {
            "model": self.cfg.model,
            "messages": [self._to_ollama(message) for message in messages],
            "stream": False,
            "options": {"temperature": self.cfg.temperature},
        }
        if tools:
            payload["tools"] = tools
        data = _post(self.cfg.base_url + "/api/chat", payload)
        message = data.get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"input": arguments}
            calls.append(
                {
                    "id": call.get("id") or "call_%s_%d" % (uuid.uuid4().hex[:6], index),
                    "name": function.get("name") or "",
                    "arguments": arguments or {},
                }
            )
        return {"content": message.get("content") or "", "tool_calls": calls}

    def _to_ollama(self, message: dict) -> dict:
        role = message.get("role", "user")
        content = message.get("content") or ""
        if role == "assistant" and message.get("tool_calls"):
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "function": {
                            "name": call["name"],
                            "arguments": call.get("arguments") or {},
                        }
                    }
                    for call in message["tool_calls"]
                ],
            }
        if role == "tool":
            return {
                "role": "tool",
                "content": content,
                "tool_name": message.get("name", ""),
            }
        return {"role": role, "content": content}

    # -- OpenAI-compatible -------------------------------------------------
    def _openai_chat(self, messages, tools) -> dict:
        payload = {
            "model": self.cfg.model,
            "messages": [self._to_openai(message) for message in messages],
            "temperature": self.cfg.temperature,
        }
        if tools:
            payload["tools"] = tools
        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = "Bearer " + self.cfg.api_key
        data = _post(self.cfg.base_url + "/chat/completions", payload, headers)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError("Unexpected response from the model server: %r" % data) from exc
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(
                {
                    "id": call.get("id") or "call_" + uuid.uuid4().hex[:8],
                    "name": function.get("name") or "",
                    "arguments": arguments or {},
                }
            )
        return {"content": message.get("content") or "", "tool_calls": calls}

    def _to_openai(self, message: dict) -> dict:
        role = message.get("role", "user")
        content = message.get("content") or ""
        if role == "assistant" and message.get("tool_calls"):
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    }
                    for call in message["tool_calls"]
                ],
            }
        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.get("tool_call_id", ""),
                "content": content,
            }
        return {"role": role, "content": content}

    # -- diagnostics -------------------------------------------------------
    def ping(self) -> dict:
        if self.cfg.provider == "ollama":
            data = _get(self.cfg.base_url + "/api/tags")
            models = [model.get("name", "") for model in data.get("models", [])]
            return {
                "ok": True,
                "models": models,
                "model_present": any(
                    name == self.cfg.model or name.startswith(self.cfg.model + ":")
                    for name in models
                ),
            }
        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = "Bearer " + self.cfg.api_key
        _get(self.cfg.base_url + "/models", headers)
        return {"ok": True, "models": [], "model_present": True}
