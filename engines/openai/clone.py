"""OpenAI 兼容的样本克隆 TTS 引擎

适用于 Unsloth Studio / audio.cpp 一类在标准 OpenAI 语音接口之外，
额外提供「上传音频输入」与「保存音色」路由的服务：

    POST {api_base}/audio/inputs?name=<文件名>   原始音频字节 -> {"id": <input_id>}
    POST {api_base}/audio/voices                 {"name", "source": {"input_id"}} -> {"id": <voice_id>}
    POST {api_base}/audio/speech                 {"model", "voice", "input", "response_format"} -> 音频二进制

首次合成时自动完成前两步并缓存 voice_id；之后只调用 /audio/speech。
音色缓存写在插件数据目录，重启后优先复用；若服务端音色已失效，
合成返回音色相关错误时会自动重新上传样本并创建一次。
"""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from astrbot.api import logger

from ..base import TTSEngine

_ALLOWED_AUDIO_FORMATS = {"mp3", "wav", "flac", "opus", "aac", "pcm"}
_SAMPLE_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}
# 服务端认为 voice 无效时，错误文本通常包含这些词。命中才自动重建音色，
# 避免把「文本/参数本身有问题」的 400 也误判成音色失效。
_VOICE_ERROR_HINTS = ("voice", "clone", "speaker", "音色", "复刻")


class _VoiceRejected(RuntimeError):
    """合成阶段服务端拒绝了 voice id（通常是保存音色已失效）。"""


class OpenAICloneEngine(TTSEngine):
    """OpenAI 兼容 + 上传样本保存音色的克隆引擎"""

    CACHE_FILE_NAME = "openai_clone_voices.json"
    DEFAULT_DATA_DIR = Path("data") / "plugin_data" / "astrbot_plugin_universal_tts"

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = str(config.get("api_key", "") or "")
        self.api_base: str = str(
            config.get("api_base", "") or "http://127.0.0.1:8888/v1"
        ).rstrip("/")
        self.model: str = str(config.get("model", "") or "").strip()
        self.transcript: str = str(config.get("transcript", "") or "").strip()
        self.voice_id_override: str = str(config.get("voice_id", "") or "").strip()
        self.audio_format: str = str(config.get("format", "mp3") or "mp3").lower().lstrip(".")
        self.timeout: int = self._coerce_timeout(config.get("timeout", 120))
        self.speed: float = self._coerce_speed(config.get("speed", 1.0))

        self._sample_paths: list[str] = list(
            (plugin_config or {}).get("voice_clone_samples", []) or []
        )
        self._sample_name: str = str(config.get("voice_sample_name", "") or "")
        self._client: httpx.AsyncClient | None = None

        # voice id 只在内存里缓存一次；首次由配置指定或通过 API 创建
        self._voice_id: str | None = self.voice_id_override or None
        self._voice_lock = asyncio.Lock()
        self._cache_key: str | None = None
        self._cache_path = self.DEFAULT_DATA_DIR / self.CACHE_FILE_NAME

        if not self.api_key:
            raise ValueError("OpenAI 克隆 TTS: api_key 不能为空")
        if self.audio_format not in _ALLOWED_AUDIO_FORMATS:
            allowed = "/".join(sorted(_ALLOWED_AUDIO_FORMATS))
            raise ValueError(f"无效的音频格式: {self.audio_format}，请使用 {allowed}")
        if not 0.25 <= self.speed <= 4.0:
            raise ValueError("OpenAI 克隆 TTS: speed 必须在 0.25 到 4.0 之间")

    @staticmethod
    def _coerce_timeout(value) -> int:
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            return 120
        return timeout if timeout > 0 else 120

    @staticmethod
    def _coerce_speed(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    # ------------------------------------------------------------------
    # HTTP 基础设施
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": content_type,
        }

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        return (response.text or "").strip()[:300]

    def _json_or_raise(self, response: httpx.Response, action: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(
                f"OpenAI 克隆 TTS: {action} 返回非有效 JSON: {self._response_detail(response)}"
            )
        if not isinstance(data, dict):
            raise RuntimeError(f"OpenAI 克隆 TTS: {action} 返回格式异常: {data!r}"[:300])
        return data

    # ------------------------------------------------------------------
    # 音色注册与缓存
    # ------------------------------------------------------------------

    def _cache_key_for(self, sample_bytes: bytes) -> str:
        """缓存键：同一服务、同一份样本内容就复用，与文件名/显示名无关。"""
        digest = hashlib.sha256(sample_bytes).hexdigest()
        return f"{self.api_base}|{digest}"

    def _voice_display_name(self, sample_path: Path) -> str:
        """保存音色的显示名默认取样本文件名，避免再让用户单独填写。"""
        name = sample_path.stem.strip()
        if not name:
            name = (self.instance_name or "universal_tts").strip()
        return name[:80] or "universal_tts"

    def _load_cache(self) -> dict:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_cache_entry(self, key: str, voice_id: str, input_id: str) -> None:
        cache = self._load_cache()
        cache[key] = {
            "voice_id": voice_id,
            "input_id": input_id,
            "updated_at": int(time.time()),
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError as e:
            logger.warning(f"[UniversalTTS] OpenAI 克隆音色缓存写入失败: {e}")

    def _remove_cache_entry(self, key: str) -> None:
        cache = self._load_cache()
        if key not in cache:
            return
        cache.pop(key, None)
        try:
            self._cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"[UniversalTTS] OpenAI 克隆音色缓存清理失败: {e}")

    def _read_sample(self) -> tuple[Path, bytes]:
        path = self._resolve_sample_path(self._sample_paths, self._sample_name)
        try:
            return path, path.read_bytes()
        except OSError as e:
            raise FileNotFoundError(f"读取音色样本失败: {path}（{e}）")

    async def _upload_input(self, sample_path: Path, sample_bytes: bytes) -> str:
        mime = _SAMPLE_MIME.get(sample_path.suffix.lower(), "application/octet-stream")
        url = f"{self.api_base}/audio/inputs?name={quote(sample_path.name)}"
        response = await self._get_client().post(
            url, headers=self._headers(mime), content=sample_bytes
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenAI 克隆 TTS 上传样本错误 [{response.status_code}]: "
                f"{self._response_detail(response)}"
            )
        data = self._json_or_raise(response, "上传样本")
        input_id = str(data.get("id") or "").strip()
        if not input_id:
            raise RuntimeError(
                f"OpenAI 克隆 TTS: 上传样本未返回 id: {self._response_detail(response)}"
            )
        return input_id

    async def _create_voice(self, input_id: str, name: str) -> str:
        payload: dict = {
            "name": name,
            "source": {"input_id": input_id},
        }
        if self.transcript:
            payload["transcript"] = self.transcript
        response = await self._get_client().post(
            f"{self.api_base}/audio/voices",
            headers=self._headers(),
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenAI 克隆 TTS 创建音色错误 [{response.status_code}]: "
                f"{self._response_detail(response)}"
            )
        data = self._json_or_raise(response, "创建音色")
        voice_id = str(data.get("id") or "").strip()
        if not voice_id:
            raise RuntimeError(
                f"OpenAI 克隆 TTS: 创建音色未返回 id: {self._response_detail(response)}"
            )
        return voice_id

    async def _ensure_voice(self) -> str:
        """返回可用的 voice id：配置指定 → 本地缓存 → 上传样本并创建。"""
        if self._voice_id:
            return self._voice_id

        async with self._voice_lock:
            if self._voice_id:
                return self._voice_id

            sample_path, sample_bytes = self._read_sample()
            key = self._cache_key_for(sample_bytes)
            self._cache_key = key

            entry = self._load_cache().get(key)
            if isinstance(entry, dict) and str(entry.get("voice_id") or "").strip():
                self._voice_id = str(entry["voice_id"]).strip()
                logger.info("[UniversalTTS] OpenAI 克隆: 复用已保存音色")
                return self._voice_id

            input_id = await self._upload_input(sample_path, sample_bytes)
            voice_id = await self._create_voice(
                input_id, self._voice_display_name(sample_path)
            )
            self._voice_id = voice_id
            self._save_cache_entry(key, voice_id, input_id)
            logger.info(
                f"[UniversalTTS] OpenAI 克隆: 已从样本 '{sample_path.name}' 创建音色"
            )
            return voice_id

    async def list_voices(self) -> list[dict]:
        """读取服务端已保存的音色列表：GET {api_base}/audio/voices。"""
        response = await self._get_client().get(
            f"{self.api_base}/audio/voices", headers=self._headers()
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenAI 克隆 TTS 读取音色列表错误 [{response.status_code}]: "
                f"{self._response_detail(response)}"
            )
        data = self._json_or_raise(response, "读取音色列表")
        raw = data.get("voices")
        if raw is None and isinstance(data.get("data"), list):
            raw = data.get("data")
        if not isinstance(raw, list):
            raise RuntimeError(
                f"OpenAI 克隆 TTS: 音色列表格式异常: {data!r}"[:300]
            )
        voices: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("id") or "").strip()
            if not voice_id:
                continue
            voices.append(
                {
                    "id": voice_id,
                    "name": str(item.get("name") or "").strip(),
                    "duration_s": item.get("duration_s"),
                    "language": item.get("language"),
                }
            )
        return voices

    async def _recreate_voice(self) -> None:
        """丢弃内存与磁盘缓存，下一次 _ensure_voice 会重新创建。"""
        async with self._voice_lock:
            self._voice_id = None
            if self._cache_key:
                self._remove_cache_entry(self._cache_key)
                self._cache_key = None

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    def _build_speech_payload(self, text: str, voice_id: str) -> dict:
        payload: dict = {
            "input": text,
            "voice": voice_id,
            "response_format": self.audio_format,
        }
        if self.model:
            payload["model"] = self.model
        if abs(self.speed - 1.0) > 1e-9:
            payload["speed"] = self.speed
        return payload

    async def _request_speech(self, text: str, voice_id: str) -> tuple[bytes, str]:
        response = await self._get_client().post(
            f"{self.api_base}/audio/speech",
            headers=self._headers(),
            json=self._build_speech_payload(text, voice_id),
        )
        if response.status_code >= 400:
            detail = self._response_detail(response)
            message = f"OpenAI 克隆 TTS 错误 [{response.status_code}]: {detail}"
            lowered = detail.lower()
            if not self.voice_id_override and (
                response.status_code == 404
                or any(hint in lowered for hint in _VOICE_ERROR_HINTS)
            ):
                raise _VoiceRejected(message)
            raise RuntimeError(message)
        return response.content, self.audio_format

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        voice_id = await self._ensure_voice()
        try:
            return await self._request_speech(text, voice_id)
        except _VoiceRejected:
            # 服务端重启或音色被删除时，缓存的 id 会失效；重建一次再试。
            logger.warning(
                "[UniversalTTS] OpenAI 克隆: 保存音色似乎已失效，重新上传样本创建"
            )
            await self._recreate_voice()
            voice_id = await self._ensure_voice()
            return await self._request_speech(text, voice_id)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
