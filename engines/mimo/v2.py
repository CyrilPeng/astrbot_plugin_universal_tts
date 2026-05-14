"""MiMo V2 TTS 引擎"""

import base64

import httpx

from ..base import TTSEngine


class MiMoV2Engine(TTSEngine):
    """小米 MiMo-V2-TTS 引擎

    API 特点：
    - 模型: mimo-v2-tts
    - 风格控制: <style>标签</style> 置于 assistant content 开头
    - 音色: 预置音色 (mimo_default, default_zh, default_en)
    - user message 为可选，可用于影响语气
    """

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.model: str = config.get("model", "mimo-v2-tts")
        self.voice: str = config.get("voice", "mimo_default")
        self.audio_format: str = config.get("format", "wav")
        self.style: str = config.get("style", "")
        self.seed_text: str = config.get("seed_text", "")
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _build_style_prefix(self, text: str) -> str:
        """构建 <style> 标签前缀"""
        if not self.style.strip():
            return text
        style_content = self.style.strip()
        if "唱歌" in style_content:
            return f"<style>唱歌</style>{text}"
        return f"<style>{style_content}</style>{text}"

    def _build_payload(self, text: str) -> dict:
        messages: list[dict[str, str]] = []

        # user message（可选，用于影响语气）
        if self.seed_text.strip():
            messages.append({"role": "user", "content": self.seed_text.strip()})

        # assistant message（待合成文本）
        assistant_content = self._build_style_prefix(text)
        messages.append({"role": "assistant", "content": assistant_content})

        return {
            "model": self.model,
            "messages": messages,
            "audio": {
                "format": self.audio_format,
                "voice": self.voice,
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
            raise RuntimeError(f"MiMo V2 TTS 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2 TTS 返回无音频数据: {data}")

        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
