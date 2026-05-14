"""自定义 HTTP TTS 引擎

通过模板化配置接入任意 HTTP TTS API。
支持占位符替换、JSON/form-data 请求体、二进制/JSON 响应解析。
"""

import base64
import json
import re

import httpx
from astrbot.api import logger

from ..base import TTSEngine

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


class CustomHTTPEngine(TTSEngine):
    """自定义 HTTP TTS 引擎"""

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.url: str = config.get("url", "")
        self.method: str = config.get("method", "POST").upper()
        self.headers_raw: str = config.get("headers", "")
        self.body_template: str = config.get("body_template", "")
        self.body_format: str = config.get("body_format", "json")
        self.response_type: str = config.get("response_type", "binary")
        self.response_audio_path: str = config.get("response_audio_path", "")
        self.response_audio_encoding: str = config.get("response_audio_encoding", "raw")
        self.audio_format: str = config.get("audio_format", "wav")
        self.api_key: str = config.get("api_key", "")
        self.timeout: int = config.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None
        self._sample_paths: list[str] = self.plugin_config.get("voice_clone_samples", [])
        self._sample_name: str = config.get("voice_sample_name", "")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _substitute(self, template: str, text: str) -> str:
        """替换模板中的占位符"""
        def _replacer(match: re.Match) -> str:
            name = match.group(1)
            if name == "TEXT":
                return text
            elif name == "API_KEY":
                if not self.api_key:
                    raise ValueError("自定义 HTTP 引擎: ${API_KEY} 占位符需要配置 api_key")
                return self.api_key
            elif name == "VOICE_SAMPLE_BASE64":
                path = self._resolve_sample_path(self._sample_paths, self._sample_name)
                return self._encode_sample_base64(path, with_prefix=True)
            elif name == "VOICE_SAMPLE_BASE64_RAW":
                path = self._resolve_sample_path(self._sample_paths, self._sample_name)
                return self._encode_sample_base64(path, with_prefix=False)
            elif name == "VOICE_SAMPLE_PATH":
                path = self._resolve_sample_path(self._sample_paths, self._sample_name)
                return str(path)
            else:
                logger.warning(f"[UniversalTTS] 自定义 HTTP: 未识别的占位符 ${{{name}}}")
                return match.group(0)
        return _PLACEHOLDER_RE.sub(_replacer, template)

    def _parse_headers(self, text: str) -> dict[str, str]:
        """解析 headers（每行 Key: Value）"""
        headers: dict[str, str] = {}
        for line in self.headers_raw.strip().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip():
                headers[key.strip()] = self._substitute(value.strip(), text)
        return headers

    def _extract_json_path(self, data, path: str) -> str:
        """从 JSON 数据中按点分路径提取值"""
        current = data
        for segment in path.split(".")[:10]:
            if isinstance(current, list):
                try:
                    current = current[int(segment)]
                except (ValueError, IndexError):
                    raise RuntimeError(f"JSON 路径 '{path}' 在 '{segment}' 处无法解析")
            elif isinstance(current, dict):
                if segment in current:
                    current = current[segment]
                else:
                    raise RuntimeError(
                        f"JSON 路径 '{path}' 在 '{segment}' 处不存在，可用键: {list(current.keys())[:10]}"
                    )
            else:
                raise RuntimeError(f"JSON 路径 '{path}' 在 '{segment}' 处遇到非容器类型")
        return current if isinstance(current, str) else str(current)

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = self._substitute(self.url, text)
        headers = self._parse_headers(text)

        # 发送请求
        if self.method == "GET":
            response = await client.request("GET", url, headers=headers)
        elif self.body_format == "form":
            form_data = {}
            for line in self.body_template.strip().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    form_data[k.strip()] = self._substitute(v.strip(), text)
            response = await client.post(url, headers=headers, data=form_data)
        else:
            body_str = self._substitute(self.body_template, text)
            headers.setdefault("Content-Type", "application/json")
            response = await client.post(url, headers=headers, content=body_str.encode("utf-8"))

        if response.status_code >= 400:
            raise RuntimeError(f"自定义 HTTP 错误 [{response.status_code}]: {response.text[:200]}")

        # 解析响应
        if self.response_type == "json":
            try:
                resp_data = response.json()
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError(f"响应非有效 JSON: {response.text[:200]}")
            if not self.response_audio_path:
                raise RuntimeError("response_type=json 但未配置 response_audio_path")
            audio_str = self._extract_json_path(resp_data, self.response_audio_path)
            if self.response_audio_encoding == "base64":
                return base64.b64decode(audio_str), self.audio_format
            return audio_str.encode("utf-8"), self.audio_format
        else:
            if self.response_audio_encoding == "base64":
                return base64.b64decode(response.content), self.audio_format
            return response.content, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
