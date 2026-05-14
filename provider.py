"""通用 TTS Provider，注入 AstrBot ProviderManager 作为 TTS 提供商"""

import uuid
from pathlib import Path

from astrbot.api import logger

from .engines import TTSEngine


class UniversalTTSProvider:
    """通用 TTS Provider

    继承 AstrBot 的 TTSProvider 接口，实现 get_audio(text) -> str。
    内部委托给具体的 TTSEngine 实例完成合成。

    注意：此类在运行时动态继承 TTSProvider，因为插件加载时
    需要确保 astrbot.core 已经可用。
    """

    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
        engine: TTSEngine,
        temp_dir: Path,
    ) -> None:
        # 调用 TTSProvider.__init__
        super().__init__(provider_config, provider_settings)
        self.engine = engine
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def get_audio(self, text: str) -> str:
        """合成音频并返回文件路径

        这是 AstrBot TTSProvider 要求实现的核心方法。
        """
        try:
            audio_bytes, fmt = await self.engine.synthesize(text)
        except Exception as e:
            logger.error(f"[UniversalTTS] 引擎 {self.engine.instance_name} 合成失败: {e}")
            raise

        output_path = self.temp_dir / f"universal_tts_{uuid.uuid4().hex}.{fmt}"
        output_path.write_bytes(audio_bytes)
        logger.debug(f"[UniversalTTS] 音频已保存: {output_path} ({len(audio_bytes)} bytes)")
        return str(output_path)

    async def terminate(self) -> None:
        """释放引擎资源"""
        await self.engine.close()


def create_provider_class():
    """动态创建继承 TTSProvider 的 Provider 类

    这样做是因为插件加载时 astrbot.core.provider.provider 必须已经可用，
    而我们不想在模块顶层 import 它（避免循环导入或加载顺序问题）。
    """
    from astrbot.core.provider.provider import TTSProvider

    class _UniversalTTSProvider(UniversalTTSProvider, TTSProvider):
        """最终的 Provider 类，同时继承 UniversalTTSProvider 和 TTSProvider"""

        def __init__(
            self,
            provider_config: dict,
            provider_settings: dict,
            engine: TTSEngine,
            temp_dir: Path,
        ) -> None:
            # 显式调用两个父类的 __init__
            TTSProvider.__init__(self, provider_config, provider_settings)
            self.engine = engine
            self.temp_dir = temp_dir
            self.temp_dir.mkdir(parents=True, exist_ok=True)

    return _UniversalTTSProvider
