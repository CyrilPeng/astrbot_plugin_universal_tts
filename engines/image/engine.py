"""Unsloth Studio 图片生成引擎

调用 OpenAI 兼容的 ``POST /v1/images/generations``：

    {
      "model": "unsloth/FLUX.2-klein-4B-GGUF",
      "prompt": "...",
      "size": "1024x1024",
      "response_format": "b64_json",
      "n": 1
    }

用户没指定尺寸时使用引擎配置的 ``size``；消息里出现 ``1024x1024``、
``16:9``、``竖图`` 等时自动覆盖。
"""

from __future__ import annotations

import base64
import re

import httpx
from astrbot.api import logger

from ..base import TTSEngine

DEFAULT_MODEL = "unsloth/FLUX.2-klein-4B-GGUF"
DEFAULT_SIZE = "1024x1024"

# 常见比例映射到 64 的倍数尺寸，避免下发模型不支持的奇数宽高。
_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1024x576",
    "9:16": "576x1024",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "3:2": "1152x768",
    "2:3": "768x1152",
    "21:9": "1344x576",
}


class UnslothImageEngine(TTSEngine):
    """调用本地 Unsloth Studio 图片模型生成图片。"""

    # 供插件钩子识别：图片引擎要绕过 TTS 概率、字数上限和跳过规则。
    is_image = True

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = str(config.get("api_key", "") or "")
        raw_base = str(
            config.get("api_base", "") or "http://127.0.0.1:8888"
        ).rstrip("/")
        if raw_base.endswith("/v1"):
            raw_base = raw_base[:-3]
        self.api_base: str = raw_base.rstrip("/")
        self.model: str = str(config.get("model", "") or DEFAULT_MODEL).strip()
        self.size: str = str(config.get("size", "") or DEFAULT_SIZE).strip()
        self.timeout: int = self._coerce_timeout(config.get("timeout", 300))
        self.audio_format: str = "png"
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("Unsloth 图片引擎: api_key 不能为空")

    @staticmethod
    def _coerce_timeout(value) -> int:
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            return 300
        return timeout if timeout > 0 else 300

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _parse_size(self, text: str) -> str:
        # 兼容中文全角冒号，例如 16：9。
        source = (text or "").replace("：", ":")
        explicit = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", source, re.I)
        if explicit:
            return f"{explicit.group(1)}x{explicit.group(2)}"
        ratio = re.search(
            r"(?i)\b(1:1|16:9|9:16|4:3|3:4|3:2|2:3|21:9)\b", source
        )
        if ratio and ratio.group(1) in _RATIO_SIZES:
            return _RATIO_SIZES[ratio.group(1)]
        lowered = source.lower()
        if any(k in lowered for k in ("横图", "横屏", "宽屏", "landscape")):
            return _RATIO_SIZES["16:9"]
        if any(k in lowered for k in ("竖图", "竖屏", "portrait", "手机壁纸")):
            return _RATIO_SIZES["9:16"]
        if any(k in lowered for k in ("方图", "正方形", "square")):
            return _RATIO_SIZES["1:1"]
        return self.size or DEFAULT_SIZE

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        size = self._parse_size(text)
        payload = {
            "model": self.model,
            "prompt": text,
            "size": size,
            "response_format": "b64_json",
            "n": 1,
        }
        client = self._get_client()
        response = await client.post(
            f"{self.api_base}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Unsloth 图片错误 [{response.status_code}]: "
                f"{(response.text or '')[:300]}"
            )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"Unsloth 图片返回非有效 JSON: {(response.text or '')[:300]}"
            )
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise RuntimeError(f"Unsloth 图片没有返回 data: {str(data)[:300]}")
        item = items[0] if isinstance(items[0], dict) else {}
        b64 = item.get("b64_json")
        if b64:
            try:
                return base64.b64decode(b64), self.audio_format
            except Exception as e:
                raise RuntimeError(f"Unsloth 图片 base64 解码失败: {e}")
        url = str(item.get("url") or "").strip()
        if url:
            if url.startswith("/"):
                url = self.api_base + url
            image = await client.get(
                url, headers={"Authorization": f"Bearer {self.api_key}"}
            )
            if image.status_code >= 400:
                raise RuntimeError(
                    f"Unsloth 图片下载错误 [{image.status_code}]: "
                    f"{(image.text or '')[:200]}"
                )
            if image.content:
                return image.content, self.audio_format
        raise RuntimeError(f"Unsloth 图片没有返回 b64_json 或 url: {str(data)[:300]}")

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
