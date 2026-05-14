"""OpenAI 兼容 TTS 引擎"""

import httpx

from .base import TTSEngine


class OpenAICompatEngine(TTSEngine):
    """兼容 OpenAI /v1/audio/speech 接口的 TTS 引擎

    适用于：
    - OpenAI TTS API
    - Azure OpenAI TTS
    - 其他兼容 OpenAI 接口的 TTS 服务
    """

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.openai.com/v1")
        self.model: str = config.get("model", "tts-1")
        self.voice: str = config.get("voice", "alloy")
        self.speed: float = config.get("speed", 1.0)
        self.audio_format: str = config.get("format", "wav")
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = f"{self.api_base.rstrip('/')}/audio/speech"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "speed": self.speed,
            "response_format": self.audio_format,
        }

        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()

        # OpenAI TTS 直接返回音频二进制流
        return response.content, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
