"""Unsloth Studio 歌曲/音乐生成引擎

固定流程：

1. 用本地 chat 模型根据**用户输入**生成 ``{"style": "...", "lyrics": "..."}``；
   ``style`` 要求英文，``lyrics`` 用 [verse]/[chorus] 组织。
2. 调用 ``POST /v1/audio/run``（``workflow=music``、``mode=song``）。
3. 下载 ``clips[]`` 的音频，交给插件挂到当前被动回复。

试听走 ``synthesize_preview()``：写死 style/lyrics，不调用 chat，只测试音乐模型效果。
"""

from __future__ import annotations

import base64
import json
import re

import httpx
from astrbot.api import logger

from ..base import TTSEngine

_MARKER_RE = re.compile(r"\[\[\s*MUSIC\s*\]\]", re.IGNORECASE)

# 固定使用本地 chat 模型生成歌词/风格，不暴露成配置项。
DEFAULT_CHAT_MODEL = "HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive:Q6_K"
DEFAULT_CHAT_TIMEOUT = 120
DEFAULT_CHAT_PROMPT = (
    "你是音乐制作助手。根据用户需求生成一首歌曲的风格提示词和歌词。"
    "style 用英文，描述流派、情绪、乐器、制作和 BPM；"
    "lyrics 用 [verse] / [chorus] 等标签组织。"
    "只输出一个 JSON 对象，不要代码块、不要解释："
    '{"style": "...", "lyrics": "..."}。'
    "如果用户要求纯器乐，lyrics 返回空字符串。"
)

# 试听：写死短样本，确保能快速验证音乐模型。
PREVIEW_STYLE = "short ambient instrumental test"
PREVIEW_LYRICS = ""
PREVIEW_DURATION = 5.0


class UnslothMusicEngine(TTSEngine):
    """调用本地 Unsloth Studio chat + 音乐模型生成歌曲/音乐。"""

    # 供插件钩子识别：音乐引擎要绕过 TTS 概率、字数上限和跳过规则。
    is_music = True

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = str(config.get("api_key", "") or "")
        raw_base = str(
            config.get("api_base", "") or "http://127.0.0.1:8888"
        ).rstrip("/")
        # 兼容用户把 OpenAI baseURL（带 /v1）直接填进来，避免拼成 /v1/v1/...
        if raw_base.endswith("/v1"):
            raw_base = raw_base[:-3]
        self.api_base: str = raw_base.rstrip("/")
        self.model: str = str(config.get("model", "") or "").strip()
        self.duration: float = self._coerce_duration(config.get("duration", 30))
        self.timeout: int = self._coerce_timeout(config.get("timeout", 1800))
        self.require_marker: bool = bool(config.get("require_marker", True))
        self.chat_model: str = DEFAULT_CHAT_MODEL
        self.chat_prompt: str = DEFAULT_CHAT_PROMPT
        self.chat_timeout: int = DEFAULT_CHAT_TIMEOUT
        self.audio_format: str = "wav"
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("Unsloth 音乐引擎: api_key 不能为空")

    @staticmethod
    def _coerce_duration(value) -> float:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            return 30.0
        return min(600.0, max(1.0, duration))

    @staticmethod
    def _coerce_timeout(value) -> int:
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            return 1800
        return timeout if timeout > 0 else 1800

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    @staticmethod
    def has_marker(text: str) -> bool:
        return bool(_MARKER_RE.search(text or ""))

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        content = (text or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        start = content.find("{")
        if start < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(content[start:])
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def _generate_with_chat(self, user_text: str) -> tuple[str, str]:
        """让 chat 模型根据用户输入生成 style/lyrics。"""
        client = self._get_client()
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": self.chat_prompt},
                {"role": "user", "content": user_text or "生成一首歌"},
            ],
            "temperature": 0.7,
        }
        try:
            response = await client.post(
                f"{self.api_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.chat_timeout,
            )
        except Exception as e:
            raise RuntimeError(f"歌词/风格 chat 模型请求失败: {e}") from e
        if response.status_code >= 400:
            raise RuntimeError(
                f"歌词/风格 chat 模型错误 [{response.status_code}]: "
                f"{(response.text or '')[:300]}"
            )
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"歌词/风格 chat 模型响应异常: {e}") from e

        parsed = self._extract_json_object(str(content))
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"歌词/风格 chat 模型未返回 JSON: {str(content)[:300]}"
            )
        style = str(parsed.get("style") or "").strip()
        lyrics = str(parsed.get("lyrics") or "").strip()
        if not style:
            raise RuntimeError("歌词/风格 chat 模型没有生成 style")
        return style, lyrics

    async def _run_music(
        self, style: str, lyrics: str, duration: float
    ) -> tuple[bytes, str]:
        payload: dict = {
            "workflow": "music",
            "mode": "song",
            "text": style,
            "duration_s": min(600.0, max(1.0, float(duration))),
            "variations": 1,
        }
        if lyrics:
            payload["lyrics"] = lyrics
        else:
            payload["instrumental"] = True
        if self.model:
            payload["model"] = self.model

        client = self._get_client()
        response = await client.post(
            f"{self.api_base}/v1/audio/run",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Unsloth 音乐错误 [{response.status_code}]: "
                f"{(response.text or '')[:300]}"
            )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"Unsloth 音乐返回非有效 JSON: {(response.text or '')[:300]}"
            )

        clips = data.get("clips") if isinstance(data, dict) else None
        if isinstance(clips, list):
            for clip in clips:
                if not isinstance(clip, dict):
                    continue
                url = str(clip.get("download_url") or "").strip()
                if url:
                    audio = await client.get(url)
                else:
                    url = str(clip.get("url") or "").strip()
                    if not url:
                        continue
                    if url.startswith("/"):
                        url = self.api_base + url
                    audio = await client.get(
                        url, headers={"Authorization": f"Bearer {self.api_key}"}
                    )
                if audio.status_code >= 400:
                    raise RuntimeError(
                        f"Unsloth 音乐下载错误 [{audio.status_code}]: "
                        f"{(audio.text or '')[:200]}"
                    )
                if audio.content:
                    return audio.content, self.audio_format

        inline = data.get("audio") if isinstance(data, dict) else None
        if isinstance(inline, dict) and inline.get("data"):
            try:
                return base64.b64decode(inline["data"]), self.audio_format
            except Exception as e:
                raise RuntimeError(f"Unsloth 音乐内联音频解码失败: {e}")

        raise RuntimeError(f"Unsloth 音乐没有返回音频: {str(data)[:300]}")

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """text 是用户输入；先生成 style/lyrics，再生成音乐。"""
        style, lyrics = await self._generate_with_chat(text)
        return await self._run_music(style, lyrics, self.duration)

    async def synthesize_preview(self) -> tuple[bytes, str]:
        """试听：写死 style/lyrics，不调用 chat。"""
        return await self._run_music(
            PREVIEW_STYLE, PREVIEW_LYRICS, PREVIEW_DURATION
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
