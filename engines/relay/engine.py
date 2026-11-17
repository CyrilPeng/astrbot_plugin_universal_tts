"""音频中转引擎

不合成语音。它从 LLM 回复文本里识别 ``[[BGM:音频路径]]`` 标记，校验路径后
把音频挂到**当前被动回复**的消息链里。适合群聊主动消息受限、但可以直接回复
语音的场景（例如 QQ 官方群）。

安全约束：
- 只允许读取 ``allowed_dirs`` 下的文件，防止任意文件读取；
- 只允许常见音频扩展名；
- 限制单文件大小。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..base import TTSEngine

_ALLOWED_EXTS = {".wav", ".mp3", ".ogg", ".opus", ".flac", ".aac", ".pcm"}
_DEFAULT_ALLOWED_DIRS = (
    Path("data") / "workspaces",
    Path("data") / "plugin_data" / "astrbot_plugin_universal_tts",
    Path("data") / "temp" / "universal_tts",
)


@dataclass
class RelayResult:
    clean_text: str
    keep_text: bool = True
    paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class AudioRelayEngine(TTSEngine):
    """把回复里的音频标记转成被动回复中的 Record。"""

    # 供插件钩子识别，避免走普通 TTS 合成流程。
    is_relay = True

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        marker = str(config.get("marker") or "BGM").strip() or "BGM"
        self.marker_name = marker
        self._marker_re = re.compile(
            r"\[\[\s*" + re.escape(marker) + r"\s*:\s*(.*?)\s*\]\]",
            re.IGNORECASE | re.DOTALL,
        )
        self.keep_text = bool(config.get("keep_text", True))
        self.max_bytes = max(1, int(config.get("max_mb", 20) or 20)) * 1024 * 1024
        try:
            self.segment_seconds = max(5, int(config.get("segment_seconds", 60) or 60))
        except (TypeError, ValueError):
            self.segment_seconds = 60
        self.allowed_dirs = self._parse_dirs(config.get("allowed_dirs"))

    @staticmethod
    def _parse_dirs(value) -> list[Path]:
        if isinstance(value, (list, tuple)):
            raw = [str(item).strip() for item in value if str(item).strip()]
        else:
            raw = [part.strip() for part in re.split(r"[\n,]+", str(value or "")) if part.strip()]
        dirs = [Path(item).expanduser() for item in raw] or list(_DEFAULT_ALLOWED_DIRS)
        return dirs

    def relay(self, text: str) -> RelayResult | None:
        """解析文本中的标记；没有标记返回 None。"""
        source = text or ""
        matches = list(self._marker_re.finditer(source))
        if not matches:
            return None

        result = RelayResult(
            clean_text=self._marker_re.sub("", source).strip(),
            keep_text=self.keep_text,
        )
        for match in matches:
            raw = match.group(1).strip().strip('"').strip("'")
            try:
                result.paths.append(self._resolve_audio(raw))
            except Exception as e:
                result.errors.append(str(e))
        return result

    def _resolve_audio(self, raw: str) -> Path:
        if not raw:
            raise ValueError("标记里没有音频路径")

        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()

        roots = [root.resolve() for root in self.allowed_dirs]
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError(f"路径不在允许目录内: {path}")
        if not path.is_file():
            raise ValueError(f"音频文件不存在: {path}")
        if path.suffix.lower() not in _ALLOWED_EXTS:
            raise ValueError(f"不支持的音频格式: {path.suffix}")
        if path.stat().st_size > self.max_bytes:
            raise ValueError(
                f"音频文件超过 {self.max_bytes // (1024 * 1024)}MB: {path}"
            )
        return path

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """接口兜底：正常流程由钩子调用 relay()，不会走到这里。"""
        result = self.relay(text)
        if result is None or not result.paths:
            raise RuntimeError("音频中转引擎未找到有效的 [[BGM:路径]] 标记")
        path = result.paths[0]
        return path.read_bytes(), path.suffix.lstrip(".").lower()
