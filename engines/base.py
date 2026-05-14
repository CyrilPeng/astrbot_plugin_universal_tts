"""TTS 引擎抽象基类"""

from abc import ABC, abstractmethod


class TTSEngine(ABC):
    """所有 TTS 引擎的抽象基类"""

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        self.config = config
        self.plugin_config = plugin_config or {}
        self.instance_name: str = config.get("instance_name", "unnamed")

    @abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """合成语音

        Args:
            text: 待合成的文本

        Returns:
            (audio_bytes, format) 元组，format 如 "wav", "mp3" 等
        """
        ...

    async def close(self) -> None:
        """释放资源，子类可选实现"""
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} instance_name={self.instance_name!r}>"
