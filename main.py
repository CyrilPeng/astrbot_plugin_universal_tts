"""通用 TTS 插件 - 通过钩子拦截消息，自行完成 TTS 合成"""

import random
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import get_engine, ENGINE_REGISTRY, TTSEngine


@register(
    "astrbot_plugin_universal_tts",
    "某不科学的高数",
    "通用 TTS 插件，支持多引擎切换，通过钩子拦截实现 TTS",
    "1.1.0",
)
class UniversalTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._engine: TTSEngine | None = None
        self._temp_dir = Path("data") / "temp" / "universal_tts"
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """插件初始化：创建引擎实例"""
        if not self.config.get("enable", True):
            logger.info("[UniversalTTS] 插件已禁用，跳过初始化")
            return

        # 获取引擎配置列表
        engines_config: list[dict] = self.config.get("engines", [])
        if not engines_config:
            logger.warning("[UniversalTTS] 未配置任何 TTS 引擎，插件不会生效")
            return

        # 根据 active_engine 选择生效的引擎
        active_engine_name = self.config.get("active_engine", "").strip()
        engine_config = None

        if active_engine_name:
            for ec in engines_config:
                if ec.get("instance_name", "") == active_engine_name:
                    engine_config = ec
                    break
            if engine_config is None:
                logger.warning(
                    f"[UniversalTTS] 未找到名为 '{active_engine_name}' 的引擎，将使用第一个"
                )

        if engine_config is None:
            engine_config = engines_config[0]

        template_key = engine_config.get("__template_key", "")
        instance_name = engine_config.get("instance_name", "").strip() or template_key

        try:
            self._engine = get_engine(engine_config, self.config)
        except Exception as e:
            logger.error(
                f"[UniversalTTS] 创建引擎失败 (类型: {template_key}, 实例: {instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            return

        logger.info(f"[UniversalTTS] 已启用，使用引擎: {instance_name} (类型: {template_key})")
        logger.info("[UniversalTTS] on_decorating_result 钩子已注册，将拦截 LLM 回复转为语音")

    @filter.on_decorating_result()
    async def tts_hook(self, event: AstrMessageEvent):
        """钩子：在消息发送前将文本转为语音"""
        if not self.config.get("enable", True):
            return
        if self._engine is None:
            return

        # TTS 触发概率
        probability = self.config.get("tts_probability", 100)
        if probability <= 0:
            return
        if probability < 100 and random.randint(1, 100) > probability:
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 只处理 LLM 产出的结果，不拦截指令回复等
        if not result.is_llm_result():
            return

        # 收集所有文本
        text_parts = []
        for component in result.chain:
            if isinstance(component, Comp.Plain) and component.text:
                text_parts.append(component.text)

        if not text_parts:
            return

        full_text = "".join(text_parts).strip()
        if len(full_text) < 1:
            return

        # 合成语音
        try:
            logger.debug(f"[UniversalTTS] 开始合成: {full_text[:50]}...")
            audio_bytes, fmt = await self._engine.synthesize(full_text)
        except Exception as e:
            logger.error(
                f"[UniversalTTS] TTS 合成失败 (引擎: {self._engine.instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            return  # 合成失败不影响原消息发送

        # 保存音频文件
        audio_path = self._temp_dir / f"tts_{uuid.uuid4().hex}.{fmt}"
        audio_path.write_bytes(audio_bytes)
        logger.debug(f"[UniversalTTS] 音频已保存: {audio_path}")

        # 替换消息链为语音
        result.chain = [Comp.Record(file=str(audio_path), url=str(audio_path))]

    @filter.command("tts_switch")
    async def switch_engine(self, event: AstrMessageEvent):
        """切换 TTS 引擎。用法: /tts_switch <引擎实例名>"""
        args = event.message_str.strip()
        if not args:
            engines_config: list[dict] = self.config.get("engines", [])
            if not engines_config:
                yield event.plain_result("未配置任何 TTS 引擎")
                return

            lines = ["当前配置的 TTS 引擎："]
            for i, ec in enumerate(engines_config):
                name = ec.get("instance_name", ec.get("__template_key", "unknown"))
                ttype = ec.get("__template_key", "")
                marker = ""
                if self._engine and self._engine.instance_name == name:
                    marker = " ← 当前"
                lines.append(f"  {i + 1}. {name} ({ttype}){marker}")

            lines.append("\n用法: /tts_switch <实例名>")
            yield event.plain_result("\n".join(lines))
            return

        # 查找目标引擎
        engines_config = self.config.get("engines", [])
        target_config = None
        for ec in engines_config:
            if ec.get("instance_name", "") == args:
                target_config = ec
                break

        if target_config is None:
            yield event.plain_result(
                f"未找到名为 '{args}' 的引擎。使用 /tts_switch 查看可用引擎。"
            )
            return

        try:
            new_engine = get_engine(target_config, self.config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return

        if self._engine:
            await self._engine.close()
        self._engine = new_engine
        logger.info(f"[UniversalTTS] 已切换引擎: {args}")
        yield event.plain_result(f"已切换 TTS 引擎为: {args}")

    @filter.command("tts_test")
    async def test_tts(self, event: AstrMessageEvent):
        """测试 TTS 合成。用法: /tts_test <文本>"""
        text = event.message_str.strip()
        if not text:
            text = "你好，这是一条 TTS 测试消息。"

        if not self._engine:
            yield event.plain_result("TTS 引擎未初始化，请检查插件配置")
            return

        try:
            audio_bytes, fmt = await self._engine.synthesize(text)
            audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
            audio_path.write_bytes(audio_bytes)
            yield event.result_message(
                [Comp.Record(file=str(audio_path), url=str(audio_path))]
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 测试合成失败: {e}")
            yield event.plain_result(f"TTS 合成失败: {e}")

    @filter.command("tts_engines")
    async def list_engines(self, event: AstrMessageEvent):
        """列出所有支持的 TTS 引擎类型"""
        lines = ["支持的 TTS 引擎类型："]
        for key in ENGINE_REGISTRY:
            lines.append(f"  • {key}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时释放引擎资源"""
        if self._engine:
            await self._engine.close()
            logger.info("[UniversalTTS] 引擎资源已释放")
