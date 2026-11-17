"""Unsloth Studio 歌曲/音乐生成引擎

直接调用 Studio 的 ``POST /v1/audio/run``：

    {
      "workflow": "music",
      "mode": "song",
      "model": "...",
      "text": "<风格/场景提示词>",
      "lyrics": "<歌词>",
      "duration_s": 60,
      "variations": 1
    }

LLM 回复可以用 ``[[MUSIC]]`` 标记 + JSON 提供风格和歌词：

    [[MUSIC]]
    {"style": "upbeat synth pop with a catchy chorus", "lyrics": "[verse]...", "duration": 60}

没有标记时，整段回复文本会被当作风格提示词；歌词和风格也可以由引擎配置兜底。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx
from astrbot.api import logger

from ..base import TTSEngine

_MARKER_RE = re.compile(r"\[\[\s*MUSIC\s*\]\]", re.IGNORECASE)
_DEFAULT_ERROR = "歌曲生成失败，请稍后重试"
DEFAULT_CHAT_PROMPT = (
    "你是音乐制作助手。根据用户需求生成一首歌曲的风格提示词和歌词。"
    "只输出一个 JSON 对象，不要代码块、不要解释："
    '{"style": "英文风格/情绪/乐器/制作提示词", '
    '"lyrics": "[verse]\\n...\\n[chorus]\\n..."}。'
    "如果用户要求纯器乐，lyrics 返回空字符串。"
)


class UnslothMusicEngine(TTSEngine):
    """调用本地 Unsloth Studio 音乐模型生成歌曲/音乐。"""

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
        self.style_prompt: str = str(config.get("style_prompt", "") or "").strip()
        self.lyrics: str = str(config.get("lyrics", "") or "").strip()
        self.duration: float = self._coerce_duration(config.get("duration", 30))
        self.timeout: int = self._coerce_timeout(config.get("timeout", 1800))
        self.require_marker: bool = bool(config.get("require_marker", True))
        # 先让 chat 模型根据用户需求生成 style/lyrics，再交给音乐模型。
        self.chat_model: str = str(config.get("chat_model", "") or "").strip()
        self.chat_prompt: str = (
            str(config.get("chat_prompt", "") or "").strip() or DEFAULT_CHAT_PROMPT
        )
        self.chat_timeout: int = self._coerce_timeout(
            config.get("chat_timeout", 120)
        )
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

    def _parse_reply(self, text: str) -> dict:
        """从回复里提取 style / lyrics / duration；没有标记时把全文当风格。"""
        source = text or ""
        style = ""
        lyrics = None
        duration = None
        model = None
        has_payload = False

        marker = _MARKER_RE.search(source)
        candidate = source[marker.end():].strip() if marker else source
        data = self._extract_json_object(candidate)
        if isinstance(data, dict):
            has_payload = True
            style = str(data.get("style") or data.get("prompt") or "").strip()
            if "lyrics" in data:
                # 显式给空歌词表示器乐，不回落到默认歌词。
                lyrics = str(data.get("lyrics") or "").strip()
            duration = data.get("duration") or data.get("duration_s")
            model = str(data.get("model") or "").strip() or None

        if not style:
            style = (source if not marker else "").strip() or self.style_prompt
        if not style:
            style = "instrumental background music"
        if lyrics is None:
            lyrics = self.lyrics
        lyrics = (lyrics or "").strip()
        if duration is None:
            duration = self.duration
        try:
            duration = min(600.0, max(1.0, float(duration)))
        except (TypeError, ValueError):
            duration = self.duration
        return {
            "style": style,
            "lyrics": lyrics,
            "duration": duration,
            "model": model or self.model,
            "has_payload": has_payload,
            "clean_text": _MARKER_RE.sub("", source).strip(),
        }

    async def _chat_generate(self, text: str) -> dict | None:
        """先让 chat 模型根据用户需求生成 style / lyrics。"""
        if not self.chat_model:
            return None
        client = self._get_client()
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": self.chat_prompt},
                {"role": "user", "content": text or "生成一首歌"},
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
            logger.warning(f"[UniversalTTS] 歌曲 chat 模型请求失败: {e}")
            return None
        if response.status_code >= 400:
            logger.warning(
                f"[UniversalTTS] 歌曲 chat 模型错误 [{response.status_code}]: "
                f"{(response.text or '')[:200]}"
            )
            return None
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning(f"[UniversalTTS] 歌曲 chat 模型响应异常: {e}")
            return None

        parsed = self._extract_json_object(str(content))
        if isinstance(parsed, dict):
            style = str(parsed.get("style") or "").strip()
            lyrics = parsed.get("lyrics")
            if style or lyrics is not None:
                return {
                    "style": style,
                    "lyrics": "" if lyrics is None else str(lyrics).strip(),
                }
        # 模型没按 JSON 输出时，把整段内容当风格，避免直接失败。
        text_content = str(content).strip()
        return {"style": text_content, "lyrics": ""} if text_content else None

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        if self.require_marker and not self.has_marker(text):
            raise RuntimeError(
                "Unsloth 音乐引擎需要 [[MUSIC]] 标记；普通文本不会触发歌曲生成"
            )
        request = self._parse_reply(text)
        # 回复里没有完整 JSON 时，先让 chat 模型按用户需求生成风格和歌词。
        if self.chat_model and not request["has_payload"]:
            generated = await self._chat_generate(request["clean_text"] or text)
            if generated:
                if generated.get("style"):
                    request["style"] = generated["style"]
                if "lyrics" in generated:
                    request["lyrics"] = generated["lyrics"]
        payload: dict = {
            "workflow": "music",
            "mode": "song",
            "text": request["style"],
            "duration_s": request["duration"],
            "variations": 1,
        }
        if request["lyrics"]:
            payload["lyrics"] = request["lyrics"]
        else:
            payload["instrumental"] = True
        if request["model"]:
            payload["model"] = request["model"]

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

        raise RuntimeError(
            f"Unsloth 音乐没有返回音频: {str(data)[:300]}"
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
