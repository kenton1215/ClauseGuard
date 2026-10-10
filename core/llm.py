# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容协议（DeepSeek / 通义千问 / 私有化 vLLM 等均可）。

设计要点：
- 统一 chat_json / chat_text 两个入口，结构化输出走 JSON mode；
- 失败自动重试并优雅降级（返回 None），规则引擎通道不受 LLM 故障影响。
"""
from __future__ import annotations

import json
import re
import time

import requests


class LLMClient:
    def __init__(self, cfg: dict):
        self.api_key = (cfg.get("api_key") or "").strip()
        self.base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.model = cfg.get("model") or "deepseek-chat"
        self.temperature = float(cfg.get("temperature", 0.2))
        self.timeout = int(cfg.get("timeout", 120))
        self.max_chunks = int(cfg.get("max_chunks", 8))
        self.enabled = bool(self.api_key)

    # ------------------------------------------------------------ 基础调用

    def _post(self, messages: list[dict], json_mode: bool, max_tokens: int | None = None) -> str | None:
        if not self.enabled:
            return None
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err = None
        for attempt in range(2):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                return None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(2 * (attempt + 1))
        print(f"[LLM] 调用失败: {last_err}")
        return None

    @staticmethod
    def _extract_json(content: str) -> dict | None:
        if not content:
            return None
        text = content.strip()
        # 剥离 markdown 代码块围栏
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 兜底：截取第一个 { 到最后一个 } 之间的内容
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None

    def chat_json(self, system: str, user: str, max_tokens: int | None = None) -> dict | None:
        content = self._post(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True, max_tokens=max_tokens)
        return self._extract_json(content)

    def chat_text(self, system: str, user: str, max_tokens: int | None = None) -> str | None:
        return self._post(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=False, max_tokens=max_tokens)
