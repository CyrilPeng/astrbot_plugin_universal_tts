"""MiMo V2.5 TTS 引擎（预置音色）"""

import base64
import re

import httpx

from ..base import TTSEngine

#: 风格串分隔符：中英文逗号、顿号、分号与空白都视为分隔。
_STYLE_SPLIT_RE = re.compile(r"[,，、;；\s]+")

#: 在风格里写这些词等同于开启唱歌模式。
_SINGING_WORDS = {"唱歌", "sing", "singing"}


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
        self.singing_mode: bool = bool(config.get("singing_mode", False))
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None

        # 参数校验
        if not self.api_key:
            raise ValueError("MiMo V2.5 TTS: api_key 不能为空")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    @staticmethod
    def _has_singing_tag(text: str) -> bool:
        """文本是否已自带 (唱歌)/(sing)/(singing) 标签，避免重复叠加。"""
        return bool(
            re.match(r"^\s*[(\[（]\s*(?:唱歌|sing|singing)\s*[)\]）]", text or "", re.IGNORECASE)
        )

    @classmethod
    def _split_styles(cls, raw: str) -> list[str]:
        """把用户输入的风格拆成词：兼容中英文逗号、顿号、分号与空白，去重保序。"""
        tokens: list[str] = []
        for token in _STYLE_SPLIT_RE.split(raw or ""):
            token = token.strip()
            if token and token not in tokens:
                tokens.append(token)
        return tokens

    def _build_style_prefix(self, text: str) -> str:
        """构建标签前缀。

        实测（2026-11-11，真实 API）：
        - ``(唱歌)歌词``            → 唱歌生效
        - ``(温柔)歌词``            → 仅风格
        - ``(唱歌 温柔)歌词``       → **唱歌失效**，官方只当普通风格解析
        - ``(唱歌)(温柔)歌词``      → 唱歌与风格同时生效
        - ``(温柔)(唱歌)歌词``      → 唱歌失效（唱歌标签必须在最前）

        所以唱歌标签必须独占最前的一个括号，风格标签另起一个括号紧随其后。
        用户只需在「风格」里用逗号分隔（如「温柔，慵懒」），括号由这里生成。
        """
        text_has_singing = self._has_singing_tag(text)
        styles: list[str] = []
        singing = self.singing_mode
        for token in self._split_styles(self.style):
            # 风格里手写「唱歌/sing/singing」等同于开启唱歌模式，且不作为风格词。
            if token.strip().lower() in _SINGING_WORDS:
                singing = True
            else:
                styles.append(token)
        if text_has_singing:
            # 文本已自带唱歌标签，视为用户自行控制，不重复追加唱歌标签。
            singing = False
        prefix = "(唱歌)" if singing else ""
        if styles:
            prefix += "(" + " ".join(styles) + ")"
        return prefix + text

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
        self._check_response("MiMo V2.5", response)

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
