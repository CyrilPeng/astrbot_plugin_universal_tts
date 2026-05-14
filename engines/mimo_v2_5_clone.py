"""MiMo V2.5 TTS VoiceClone 引擎"""

import base64
from pathlib import Path

import httpx

from .base import TTSEngine


class MiMoV25CloneEngine(TTSEngine):
    """小米 MiMo-V2.5-TTS-VoiceClone 引擎

    API 特点：
    - 模型: mimo-v2.5-tts-voiceclone
    - 通过音频样本复刻音色
    - voice 字段传入 base64 编码的音频样本
    - 支持 mp3 和 wav 格式样本

    配置中 voice_sample 为 AstrBot file 类型配置项，
    值为文件路径列表（用户通过 WebUI 上传）。
    """

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.audio_format: str = config.get("format", "wav")
        self.style_prompt: str = config.get("style_prompt", "")
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None
        self._voice_base64: str | None = None

        # 顶层配置
        self._voice_sample_paths: list[str] = self.plugin_config.get("voice_clone_samples", [])
        # 引擎条目中用户指定的样本文件名
        self._voice_sample_name: str = config.get("voice_sample_name", "")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _resolve_sample_path(self) -> Path:
        """解析音色样本文件路径

        AstrBot 的 file 类型配置存储的是相对路径（如 files/xxx/yyy.wav），
        实际文件位于 AstrBot data 目录下。
        """
        if not self._voice_sample_paths:
            raise FileNotFoundError(
                "未上传音色样本文件，请在插件配置中上传 mp3 或 wav 格式的音频文件"
            )

        # 如果用户未指定文件名，使用第一个
        if not self._voice_sample_name.strip():
            raw_path = self._voice_sample_paths[0]
        else:
            # 按文件名匹配
            target_name = self._voice_sample_name.strip()
            raw_path = None
            for p in self._voice_sample_paths:
                if Path(p).name == target_name:
                    raw_path = p
                    break
            if raw_path is None:
                available = ", ".join(Path(p).name for p in self._voice_sample_paths)
                raise FileNotFoundError(
                    f"未找到名为 '{target_name}' 的样本文件。"
                    f"已上传的文件: {available}"
                )

        # 尝试多种路径解析方式
        candidates = [
            Path(raw_path),                          # 原始路径
            Path("data") / "plugin_data" / "astrbot_plugin_universal_tts" / raw_path,
            Path.cwd() / "data" / "plugin_data" / "astrbot_plugin_universal_tts" / raw_path,
            Path("data") / raw_path,                 # data/ 前缀
            Path.cwd() / raw_path,                   # 工作目录 + 相对路径
            Path.cwd() / "data" / raw_path,          # 工作目录 + data/ + 相对路径
        ]

        # 尝试通过 astrbot 工具获取路径
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_path
            astrbot_data = Path(get_astrbot_path())
            candidates.append(astrbot_data / raw_path)
            candidates.append(astrbot_data / "plugin_data" / "astrbot_plugin_universal_tts" / raw_path)
        except Exception:
            pass

        for candidate in candidates:
            if candidate.exists():
                return candidate

        raise FileNotFoundError(
            f"音色样本文件不存在: {raw_path}。"
            f"尝试过的路径: {', '.join(str(c) for c in candidates)}。"
            "文件可能已被删除，请重新上传。"
        )

    def _get_voice_base64(self) -> str:
        """读取音频样本并编码为 base64"""
        if self._voice_base64 is not None:
            return self._voice_base64

        sample_path = self._resolve_sample_path()

        suffix = sample_path.suffix.lower()
        mime_map = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
        }
        mime_type = mime_map.get(suffix)
        if mime_type is None:
            raise ValueError(f"不支持的音频格式: {suffix}，仅支持 mp3/wav")

        audio_bytes = sample_path.read_bytes()
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        self._voice_base64 = f"data:{mime_type};base64,{encoded}"
        return self._voice_base64

    def _build_payload(self, text: str) -> dict:
        messages: list[dict[str, str]] = []

        # user message: 自然语言风格指令（可选）
        if self.style_prompt.strip():
            messages.append({"role": "user", "content": self.style_prompt.strip()})
        else:
            messages.append({"role": "user", "content": ""})

        # assistant message: 待合成文本
        messages.append({"role": "assistant", "content": text})

        return {
            "model": "mimo-v2.5-tts-voiceclone",
            "messages": messages,
            "audio": {
                "format": self.audio_format,
                "voice": self._get_voice_base64(),
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
            raise RuntimeError(f"MiMo V2.5 VoiceClone 返回空 choices: {data}")

        audio_data = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_data:
            raise RuntimeError(f"MiMo V2.5 VoiceClone 返回无音频数据: {data}")

        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
