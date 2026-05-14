"""阿里云百炼 CosyVoice TTS 引擎

基于阿里云百炼 DashScope 兼容接口：
- CosyVoice 模型仅支持 WebSocket 连接
- 本引擎使用 DashScope OpenAI 兼容接口（/v1/services/aigc/text-generation/generation）
- 鉴权: Authorization: Bearer <api_key>
- 响应: JSON，output.audio 字段为 base64 编码的音频

注意：阿里云百炼 CosyVoice 官方推荐使用 WebSocket SDK，
本引擎使用 HTTP 兼容接口以保持统一架构。
"""

import base64

import httpx

from ..base import TTSEngine


class AliyunEngine(TTSEngine):
    """阿里云百炼 CosyVoice TTS 引擎"""

    API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", self.API_BASE)
        self.model: str = config.get("model", "cosyvoice-v1")
        self.voice: str = config.get("voice", "longxiaochun")
        self.audio_format: str = config.get("format", "wav")
        self.volume: int = config.get("volume", 50)
        self.speech_rate: float = config.get("speech_rate", 1.0)
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("阿里云百炼 TTS: api_key 不能为空")

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
            "response_format": self.audio_format,
            "speed": self.speech_rate,
            "volume": self.volume,
        }

        response = await client.post(url, headers=headers, json=payload)

        if response.status_code >= 400:
            body_text = response.text[:200]
            raise RuntimeError(
                f"阿里云百炼 TTS 错误 [{response.status_code}]: {body_text}"
            )

        # 阿里云百炼 OpenAI 兼容接口直接返回音频二进制流
        return response.content, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
