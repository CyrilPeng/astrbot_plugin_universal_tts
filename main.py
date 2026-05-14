"""通用 TTS 插件 - 注册为 AstrBot TTS Provider"""

from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import get_engine, ENGINE_REGISTRY
from .provider import create_provider_class


@register(
    "astrbot_plugin_universal_tts",
    "某不科学的高数",
    "通用 TTS 插件，支持多引擎切换，注册为 AstrBot TTS Provider",
    "1.0.0",
)
class UniversalTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._provider_inst = None
        self._provider_id = "universal_tts_plugin"

    async def initialize(self):
        """插件初始化：创建引擎实例并注入 ProviderManager"""
        if not self.config.get("enable", True):
            logger.info("[UniversalTTS] 插件已禁用，跳过初始化")
            return

        # 获取引擎配置列表
        engines_config: list[dict] = self.config.get("engines", [])
        if not engines_config:
            logger.warning("[UniversalTTS] 未配置任何 TTS 引擎，插件不会生效")
            return

        # 使用第一个引擎作为默认
        engine_config = engines_config[0]
        template_key = engine_config.get("__template_key", "")
        instance_name = engine_config.get("instance_name", template_key)

        try:
            engine = get_engine(engine_config)
        except Exception as e:
            logger.error(f"[UniversalTTS] 创建引擎失败: {e}")
            return

        logger.info(f"[UniversalTTS] 使用引擎: {instance_name} (类型: {template_key})")

        # 创建 Provider 实例
        ProviderClass = create_provider_class()
        temp_dir = Path("data") / "temp" / "universal_tts"

        provider_config = {
            "id": self._provider_id,
            "type": "universal_tts_plugin",
            "enable": True,
        }

        self._provider_inst = ProviderClass(
            provider_config=provider_config,
            provider_settings={},
            engine=engine,
            temp_dir=temp_dir,
        )

        # 注入到 ProviderManager
        try:
            pm = self.context.provider_manager
            pm.tts_provider_insts.append(self._provider_inst)
            pm.inst_map[self._provider_id] = self._provider_inst

            # 设为默认 TTS Provider
            if self.config.get("set_as_default", True):
                pm.curr_tts_provider_inst = self._provider_inst
                logger.info(
                    f"[UniversalTTS] 已设为默认 TTS Provider (引擎: {instance_name})"
                )
            else:
                logger.info(
                    f"[UniversalTTS] Provider 已注册 (ID: {self._provider_id})，"
                    "请在 AstrBot 设置中手动选择"
                )
        except Exception as e:
            logger.error(f"[UniversalTTS] 注入 ProviderManager 失败: {e}")
            return

    @filter.command("tts_switch")
    async def switch_engine(self, event: AstrMessageEvent):
        """切换 TTS 引擎。用法: /tts_switch <引擎实例名>"""
        args = event.message_str.strip()
        if not args:
            # 列出当前配置的引擎
            engines_config: list[dict] = self.config.get("engines", [])
            if not engines_config:
                yield event.plain_result("未配置任何 TTS 引擎")
                return

            lines = ["当前配置的 TTS 引擎："]
            for i, ec in enumerate(engines_config):
                name = ec.get("instance_name", ec.get("__template_key", "unknown"))
                ttype = ec.get("__template_key", "")
                marker = " ← 当前" if i == 0 and self._provider_inst else ""
                if self._provider_inst and self._provider_inst.engine.instance_name == name:
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

        # 切换引擎
        try:
            new_engine = get_engine(target_config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return

        # 关闭旧引擎
        if self._provider_inst:
            await self._provider_inst.engine.close()
            self._provider_inst.engine = new_engine
            logger.info(f"[UniversalTTS] 已切换引擎: {args}")
            yield event.plain_result(f"已切换 TTS 引擎为: {args}")
        else:
            yield event.plain_result("TTS Provider 未初始化，请检查插件配置")

    @filter.command("tts_test")
    async def test_tts(self, event: AstrMessageEvent):
        """测试 TTS 合成。用法: /tts_test <文本>"""
        text = event.message_str.strip()
        if not text:
            text = "你好，这是一条 TTS 测试消息。"

        if not self._provider_inst:
            yield event.plain_result("TTS Provider 未初始化，请检查插件配置")
            return

        try:
            from astrbot.core.message.components import Record

            audio_path = await self._provider_inst.get_audio(text)
            yield event.result_message([Record(file=audio_path, url=audio_path, text=text)])
        except Exception as e:
            logger.error(f"[UniversalTTS] 测试合成失败: {e}")
            yield event.plain_result(f"TTS 合成失败: {e}")

    @filter.command("tts_engines")
    async def list_engines(self, event: AstrMessageEvent):
        """列出所有支持的 TTS 引擎类型"""
        lines = ["支持的 TTS 引擎类型："]
        engine_descriptions = {
            "mimo_v2": "MiMo-V2-TTS（预置音色 + style 标签）",
            "mimo_v2_5": "MiMo-V2.5-TTS（精品音色 + 自然语言控制）",
            "mimo_v2_5_voicedesign": "MiMo-V2.5-VoiceDesign（文本描述设计音色）",
            "mimo_v2_5_voiceclone": "MiMo-V2.5-VoiceClone（音频样本克隆音色）",
            "openai_compat": "OpenAI 兼容 TTS（/v1/audio/speech）",
        }
        for key in ENGINE_REGISTRY:
            desc = engine_descriptions.get(key, key)
            lines.append(f"  • {key}: {desc}")

        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载时清理 Provider"""
        if self._provider_inst:
            try:
                pm = self.context.provider_manager
                # 从 ProviderManager 中移除
                if self._provider_inst in pm.tts_provider_insts:
                    pm.tts_provider_insts.remove(self._provider_inst)
                if self._provider_id in pm.inst_map:
                    del pm.inst_map[self._provider_id]
                if pm.curr_tts_provider_inst is self._provider_inst:
                    # 恢复为列表中的第一个（如果有）
                    pm.curr_tts_provider_inst = (
                        pm.tts_provider_insts[0] if pm.tts_provider_insts else None
                    )
                await self._provider_inst.terminate()
                logger.info("[UniversalTTS] Provider 已从 ProviderManager 中移除")
            except Exception as e:
                logger.error(f"[UniversalTTS] 清理 Provider 失败: {e}")
