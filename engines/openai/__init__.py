"""OpenAI 兼容 TTS 引擎"""

from .clone import OpenAICloneEngine
from .compat import OpenAICompatEngine

__all__ = ["OpenAICompatEngine", "OpenAICloneEngine"]
