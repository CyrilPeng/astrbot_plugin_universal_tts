"""火山引擎 TTS 引擎

基于火山引擎音频技术 SAMI HTTP API：
- 端点: POST https://sami.bytedance.com/api/v1/invoke
- 鉴权: query string 中带 token 和 appkey
- 请求: JSON body，payload 字段为序列化的 JSON 字符串
- 响应: JSON，data 字段为 base64 编码的音频数据
"""

import base64
import json

import httpx

from ..base import TTSEngine


class VolcengineEngine(TTSEngine):
    """火山引擎 TTS 引擎

    使用 SAMI HTTP API 进行语音合成。
    """

    API_BASE = "https://sami.bytedance.com"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.app_id: str = config.get("app_id", "")
        self.access_token: str = config.get("access_token", "")
        self.voice_type: str = config.get("voice_type", "zh_female_qingxin")
        self.speed_ratio: float = config.get("speed_ratio", 1.0)
        self.volume_ratio: float = config.get("volume_ratio", 1.0)
        self.audio_format: str = config.get("format", "mp3")
        self.sample_rate: int = config.get("sample_rate", 24000)
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

        # 参数校验
        if not self.app_id:
            raise ValueError("火山引擎 TTS: app_id 不能为空")
        if not self.access_token:
            raise ValueError("火山引擎 TTS: access_token 不能为空")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _build_payload(self, text: str) -> dict:
        """构建请求体"""
        tts_payload = {
            "text": text,
            "speaker": self.voice_type,
            "audio_config": {
                "format": self.audio_format,
                "sample_rate": self.sample_rate,
                "speech_rate": int((self.speed_ratio - 1.0) * 100),  # 转换为 [-50, 100] 范围
            },
        }

        return {
            "payload": json.dumps(tts_payload, ensure_ascii=False),
        }

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()

        url = (
            f"{self.API_BASE}/api/v1/invoke"
            f"?version=v4"
            f"&token={self.access_token}"
            f"&appkey={self.app_id}"
            f"&namespace=TTS"
        )

        headers = {
            "Content-Type": "application/json",
        }

        response = await client.post(
            url, headers=headers, json=self._build_payload(text)
        )
        response.raise_for_status()

        data = response.json()

        # 检查状态码
        status_code = data.get("status_code", 0)
        if status_code != 20000000:
            status_text = data.get("status_text", "未知错误")
            raise RuntimeError(
                f"火山引擎 TTS 错误 [{status_code}]: {status_text}"
            )

        # 提取音频数据（base64 编码）
        audio_b64 = data.get("data")
        if not audio_b64:
            raise RuntimeError(f"火山引擎 TTS 返回无音频数据: {data}")

        audio_bytes = base64.b64decode(audio_b64)
        return audio_bytes, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
