"""TTS 引擎工厂模块"""

from .base import TTSEngine
from .mimo_v2 import MiMoV2Engine
from .mimo_v2_5 import MiMoV25Engine
from .mimo_v2_5_design import MiMoV25DesignEngine
from .mimo_v2_5_clone import MiMoV25CloneEngine
from .openai_compat import OpenAICompatEngine

# 引擎类型名 -> 引擎类 的映射
ENGINE_REGISTRY: dict[str, type[TTSEngine]] = {
    "mimo_v2": MiMoV2Engine,
    "mimo_v2_5": MiMoV25Engine,
    "mimo_v2_5_voicedesign": MiMoV25DesignEngine,
    "mimo_v2_5_voiceclone": MiMoV25CloneEngine,
    "openai_compat": OpenAICompatEngine,
}


def get_engine(engine_config: dict) -> TTSEngine:
    """根据配置创建引擎实例

    Args:
        engine_config: 单个引擎的配置字典（来自 template_list 中的一项）

    Returns:
        TTSEngine 实例
    """
    template_key = engine_config.get("__template_key", "")
    engine_cls = ENGINE_REGISTRY.get(template_key)
    if engine_cls is None:
        available = ", ".join(ENGINE_REGISTRY.keys())
        raise ValueError(
            f"未知的 TTS 引擎类型: '{template_key}'，可用类型: {available}"
        )
    return engine_cls(engine_config)


__all__ = [
    "TTSEngine",
    "ENGINE_REGISTRY",
    "get_engine",
]
