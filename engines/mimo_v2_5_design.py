"""MiMo V2.5 TTS VoiceDesign 引擎"""

import base64

import httpx

from .base import TTSEngine


class MiMoV25DesignEngine(TTSEngine):
    """小米 MiMo-V2.5-TTS-VoiceDesign 引擎

    API 特点：
    - 模型: mimo-v2.5-tts-voicedesign
    - 通过 user message 中的文本描述来设计音色
    - 不使用预置音色，不支持音色克隆
    - 可选 optimize_text_preview 参数智能润色文本
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.voice_description: str = config.get("voice_description", "")
        self.audio_format: str = config.get("format", "wav")
        self.optimize_text_preview: bool = config.get("optimize_text_preview", False)
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _build_payload(self, text: str) -> dict:
        messages: list[dict[str, str]] = []

        # user message: 音色描述（必填）
        voice_desc = self.voice_description.strip()
        if not voice_desc:
            voice_desc = "A natural and clear voice."
        messages.append({"role": "user", "content": voice_desc})

        # assistant message: 待合成文本
        messages.append({"role": "assistant", "content": text})

        payload: dict = {
            "model": "mimo-v2.5-tts-voicedesign",
            "messages": messages,
            "audio": {
                "format": self.audio_format,
                "optimize_text_preview": self.optimize_text_preview,
            },
        }
        return payload

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
            raise RuntimeError(f"MiMo V2.5 VoiceDesign 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2.5 VoiceDesign 返回无音频数据: {data}")

        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
