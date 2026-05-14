"""MiMo V2.5 TTS VoiceClone 引擎"""

import base64
from pathlib import Path

import httpx

from .base import TTSEngine


class MiMoV25CloneEngine(TTSEngine):
    """小米 MiMo-V2.5-TTS-VoiceClone 引擎

    API 特点：
    - 模型: mimo-v2.5-tts-voiceclone
    - 通过音频样本复刻音色
    - voice 字段传入 base64 编码的音频样本
    - 支持 mp3 和 wav 格式样本
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.voice_sample_path: str = config.get("voice_sample_path", "")
        self.audio_format: str = config.get("format", "wav")
        self.style_prompt: str = config.get("style_prompt", "")
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None
        self._voice_base64: str | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _get_voice_base64(self) -> str:
        """读取音频样本并编码为 base64"""
        if self._voice_base64 is not None:
            return self._voice_base64

        sample_path = Path(self.voice_sample_path)
        if not sample_path.exists():
            raise FileNotFoundError(f"音色样本文件不存在: {self.voice_sample_path}")

        suffix = sample_path.suffix.lower()
        mime_map = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
        }
        mime_type = mime_map.get(suffix)
        if mime_type is None:
            raise ValueError(f"不支持的音频格式: {suffix}，仅支持 mp3/wav")

        audio_bytes = sample_path.read_bytes()
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        self._voice_base64 = f"data:{mime_type};base64,{encoded}"
        return self._voice_base64

    def _build_payload(self, text: str) -> dict:
        messages: list[dict[str, str]] = []

        # user message: 自然语言风格指令（可选）
        if self.style_prompt.strip():
            messages.append({"role": "user", "content": self.style_prompt.strip()})
        else:
            messages.append({"role": "user", "content": ""})

        # assistant message: 待合成文本
        messages.append({"role": "assistant", "content": text})

        return {
            "model": "mimo-v2.5-tts-voiceclone",
            "messages": messages,
            "audio": {
                "format": self.audio_format,
                "voice": self._get_voice_base64(),
            },
        }

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

        response = await client.post(url, headers=headers, json=self._build_payload(text))
        response.raise_for_status()

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"MiMo V2.5 VoiceClone 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2.5 VoiceClone 返回无音频数据: {data}")

        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
