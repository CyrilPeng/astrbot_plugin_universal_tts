"""MiMo V2.5 TTS 引擎（预置音色）"""

import base64

import httpx

from ..base import TTSEngine


class MiMoV25Engine(TTSEngine):
    """小米 MiMo-V2.5-TTS 引擎

    API 特点：
    - 模型: mimo-v2.5-tts
    - 风格控制:
      - 自然语言控制: 放在 user message 的 content 中
      - 音频标签控制: (风格) 置于 assistant content 开头
    - 音色: 更多精品预置音色
    - user message 支持自然语言风格指令
    """

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.model: str = config.get("model", "mimo-v2.5-tts")
        self.voice: str = config.get("voice", "mimo_default")
        self.audio_format: str = config.get("format", "wav")
        self.style: str = config.get("style", "")
        self.style_prompt: str = config.get("style_prompt", "")
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _build_style_prefix(self, text: str) -> str:
        """构建 (风格) 标签前缀（V2.5 使用圆括号）"""
        if not self.style.strip():
            return text
        style_content = self.style.strip()
        if "唱歌" in style_content:
            return f"(唱歌){text}"
        return f"({style_content}){text}"

    def _build_payload(self, text: str) -> dict:
        messages: list[dict[str, str]] = []

        # user message: 自然语言风格指令
        if self.style_prompt.strip():
            messages.append({"role": "user", "content": self.style_prompt.strip()})

        # assistant message: 待合成文本（可带标签风格前缀）
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
            raise RuntimeError(f"MiMo V2.5 TTS 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2.5 TTS 返回无音频数据: {data}")

        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
