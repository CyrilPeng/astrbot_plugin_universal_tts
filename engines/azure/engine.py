"""Azure Cognitive Services TTS 引擎

基于 Azure Speech Service REST API：
- 端点: POST https://{region}.tts.speech.microsoft.com/cognitiveservices/v1
- 鉴权: Ocp-Apim-Subscription-Key header
- 请求: SSML XML body
- 响应: 直接返回音频二进制流
"""

import httpx

from ..base import TTSEngine


# Azure 输出格式到文件扩展名的映射
_FORMAT_TO_EXT = {
    "audio-16khz-128kbitrate-mono-mp3": "mp3",
    "audio-16khz-64kbitrate-mono-mp3": "mp3",
    "audio-16khz-32kbitrate-mono-mp3": "mp3",
    "audio-24khz-160kbitrate-mono-mp3": "mp3",
    "audio-24khz-96kbitrate-mono-mp3": "mp3",
    "audio-24khz-48kbitrate-mono-mp3": "mp3",
    "audio-48khz-192kbitrate-mono-mp3": "mp3",
    "audio-48khz-96kbitrate-mono-mp3": "mp3",
    "riff-16khz-16bit-mono-pcm": "wav",
    "riff-24khz-16bit-mono-pcm": "wav",
    "riff-48khz-16bit-mono-pcm": "wav",
    "ogg-16khz-16bit-mono-opus": "ogg",
    "ogg-24khz-16bit-mono-opus": "ogg",
    "ogg-48khz-16bit-mono-opus": "ogg",
    "raw-16khz-16bit-mono-pcm": "pcm",
    "raw-24khz-16bit-mono-pcm": "pcm",
    "raw-48khz-16bit-mono-pcm": "pcm",
}


class AzureEngine(TTSEngine):
    """Azure Cognitive Services TTS 引擎"""

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.subscription_key: str = config.get("subscription_key", "")
        self.region: str = config.get("region", "")
        self.voice_name: str = config.get("voice_name", "zh-CN-XiaoxiaoNeural")
        self.output_format: str = config.get(
            "output_format", "audio-16khz-128kbitrate-mono-mp3"
        )
        self.style: str = config.get("style", "")
        self.role: str = config.get("role", "")
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None

        if not self.subscription_key:
            raise ValueError("Azure TTS: subscription_key 不能为空")
        if not self.region:
            raise ValueError("Azure TTS: region 不能为空")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _build_ssml(self, text: str) -> str:
        """构建 SSML 请求体"""
        # 转义 XML 特殊字符
        escaped_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

        # 构建 express-as 标签（如果有 style 或 role）
        if self.style or self.role:
            style_attr = f' style="{self.style}"' if self.style else ""
            role_attr = f' role="{self.role}"' if self.role else ""
            content = (
                f"<mstts:express-as{style_attr}{role_attr}>"
                f"{escaped_text}"
                f"</mstts:express-as>"
            )
        else:
            content = escaped_text

        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" '
            f'xml:lang="zh-CN">'
            f'<voice name="{self.voice_name}">'
            f"{content}"
            f"</voice></speak>"
        )
        return ssml

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = (
            f"https://{self.region}.tts.speech.microsoft.com"
            f"/cognitiveservices/v1"
        )

        headers = {
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": self.output_format,
            "User-Agent": "AstrBot-UniversalTTS",
        }

        ssml_body = self._build_ssml(text)
        response = await client.post(url, headers=headers, content=ssml_body)

        if response.status_code >= 400:
            body_text = response.text[:200]
            raise RuntimeError(
                f"Azure TTS 错误 [{response.status_code}]: {body_text}"
            )

        # 从输出格式推断文件扩展名
        ext = _FORMAT_TO_EXT.get(self.output_format, "mp3")
        return response.content, ext

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
