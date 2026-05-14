"""TTS 引擎工厂模块"""

from .base import TTSEngine
from .mimo import MiMoV2Engine, MiMoV25Engine, MiMoV25DesignEngine, MiMoV25CloneEngine
from .openai import OpenAICompatEngine
from .volcengine import VolcengineEngine
from .aliyun import AliyunEngine
from .azure import AzureEngine
from .elevenlabs import ElevenLabsEngine
from .custom_http import CustomHTTPEngine

ENGINE_REGISTRY: dict[str, type[TTSEngine]] = {
    "mimo_v2": MiMoV2Engine,
    "mimo_v2_5": MiMoV25Engine,
    "mimo_v2_5_voicedesign": MiMoV25DesignEngine,
    "mimo_v2_5_voiceclone": MiMoV25CloneEngine,
    "openai_compat": OpenAICompatEngine,
    "volcengine": VolcengineEngine,
    "aliyun": AliyunEngine,
    "azure": AzureEngine,
    "elevenlabs": ElevenLabsEngine,
    "custom_http": CustomHTTPEngine,
}


def get_engine(engine_config: dict, plugin_config: dict | None = None) -> TTSEngine:
    """根据配置创建引擎实例"""
    template_key = engine_config.get("__template_key", "")
    engine_cls = ENGINE_REGISTRY.get(template_key)
    if engine_cls is None:
        available = ", ".join(ENGINE_REGISTRY.keys())
        raise ValueError(f"未知的 TTS 引擎类型: '{template_key}'，可用类型: {available}")
    return engine_cls(engine_config, plugin_config or {})
