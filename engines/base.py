"""TTS 引擎抽象基类"""

import base64
from abc import ABC, abstractmethod
from pathlib import Path


class TTSEngine(ABC):
    """所有 TTS 引擎的抽象基类"""

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        self.config = config
        self.plugin_config = plugin_config or {}
        self.instance_name: str = config.get("instance_name", "") or config.get("__template_key", "unnamed")

    @abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """合成语音，返回 (audio_bytes, format)"""
        ...

    async def close(self) -> None:
        """释放资源，子类可选实现"""
        pass

    def _resolve_sample_path(self, sample_paths: list[str], sample_name: str = "") -> Path:
        """解析音色样本文件路径（共用逻辑）"""
        if not sample_paths:
            raise FileNotFoundError("未上传音色样本文件，请在插件配置中上传 mp3 或 wav 格式的音频文件")

        if not sample_name.strip():
            raw_path = sample_paths[0]
        else:
            raw_path = next(
                (p for p in sample_paths if Path(p).name == sample_name.strip()),
                None,
            )
            if raw_path is None:
                available = ", ".join(Path(p).name for p in sample_paths)
                raise FileNotFoundError(f"未找到名为 '{sample_name}' 的样本文件。已上传: {available}")

        candidates = [
            Path(raw_path),
            Path("data") / "plugin_data" / "astrbot_plugin_universal_tts" / raw_path,
            Path("data") / raw_path,
            Path.cwd() / raw_path,
        ]
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_path
            candidates.append(Path(get_astrbot_path()) / raw_path)
        except Exception:
            pass

        for c in candidates:
            if c.exists():
                return c

        raise FileNotFoundError(f"音色样本文件不存在: {raw_path}，文件可能已被删除，请重新上传。")

    def _encode_sample_base64(self, sample_path: Path, with_prefix: bool = True) -> str:
        """将音色样本编码为 base64"""
        audio_bytes = sample_path.read_bytes()
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        if not with_prefix:
            return encoded
        mime_map = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}
        mime_type = mime_map.get(sample_path.suffix.lower(), "application/octet-stream")
        return f"data:{mime_type};base64,{encoded}"
