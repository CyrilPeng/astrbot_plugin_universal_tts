"""ElevenLabs TTS 引擎

基于 ElevenLabs Text-to-Speech API：
- 端点: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
- 鉴权: xi-api-key header
- 请求: JSON body (text, model_id, voice_settings)
- 响应: 直接返回音频二进制流
"""

import httpx

from ..base import TTSEngine


# ElevenLabs output_format 到文件扩展名的映射
_FORMAT_TO_EXT = {
    "mp3_44100_128": "mp3",
    "mp3_44100_192": "mp3",
    "mp3_44100_64": "mp3",
    "mp3_22050_32": "mp3",
    "pcm_16000": "pcm",
    "pcm_22050": "pcm",
    "pcm_24000": "pcm",
    "pcm_44100": "pcm",
    "ulaw_8000": "wav",
}


class ElevenLabsEngine(TTSEngine):
    """ElevenLabs TTS 引擎"""

    API_BASE = "https://api.elevenlabs.io/v1"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.voice_id: str = config.get("voice_id", "")
        self.model_id: str = config.get("model_id", "eleven_multilingual_v2")
        self.stability: float = config.get("stability", 0.5)
        self.similarity_boost: float = config.get("similarity_boost", 0.75)
        self.output_format: str = config.get("output_format", "mp3_44100_128")
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("ElevenLabs TTS: api_key 不能为空")
        if not self.voice_id:
            raise ValueError("ElevenLabs TTS: voice_id 不能为空")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = (
            f"{self.API_BASE}/text-to-speech/{self.voice_id}"
            f"?output_format={self.output_format}"
        )

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
            },
        }

        response = await client.post(url, headers=headers, json=payload)

        if response.status_code >= 400:
            body_text = response.text[:200]
            raise RuntimeError(
                f"ElevenLabs TTS 错误 [{response.status_code}]: {body_text}"
            )

        ext = _FORMAT_TO_EXT.get(self.output_format, "mp3")
        return response.content, ext

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
