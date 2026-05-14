"""通用 TTS 插件 - 通过钩子拦截消息，自行完成 TTS 合成"""

import json
import random
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import get_engine, TTSEngine


def _is_slash(event: AstrMessageEvent) -> bool:
    """判断原始消息是否以斜杠开头（强制要求斜杠指令）"""
    raw = getattr(event.message_obj, "message_str", "") or ""
    return raw.lstrip().startswith("/")


def _get_cmd_args(event: AstrMessageEvent) -> str:
    """从 message_str 中提取命令参数（去掉命令名本身）"""
    text = event.message_str.strip()
    # message_str 格式为 "command_name args..."，需要去掉命令名
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _get_session_id(event: AstrMessageEvent) -> str:
    """获取当前会话的唯一标识（UMO）"""
    return getattr(event, "unified_msg_origin", "") or ""


@register(
    "astrbot_plugin_universal_tts",
    "某不科学的高数",
    "通用 TTS 插件，支持多引擎切换，通过钩子拦截实现 TTS",
    "1.2.1",
)
class UniversalTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._engine: TTSEngine | None = None
        self._session_engines: dict[str, TTSEngine] = {}  # session_id -> engine
        self._session_bindings: dict[str, str] = {}  # session_id -> instance_name
        self._temp_dir = Path("data") / "temp" / "universal_tts"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._bindings_file = Path("data") / "plugin_data" / "astrbot_plugin_universal_tts" / "session_bindings.json"
        self._bindings_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_bindings(self):
        """从文件加载会话绑定"""
        if self._bindings_file.exists():
            try:
                self._session_bindings = json.loads(self._bindings_file.read_text(encoding="utf-8"))
            except Exception:
                self._session_bindings = {}

    def _save_bindings(self):
        """持久化会话绑定到文件"""
        try:
            self._bindings_file.write_text(
                json.dumps(self._session_bindings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 保存会话绑定失败: {e}")

    def _find_engine_config(self, name_or_idx: str) -> dict | None:
        """按序号或实例名查找引擎配置"""
        engines_config = self._list_engines()
        if name_or_idx.isdigit():
            idx = int(name_or_idx)
            if 1 <= idx <= len(engines_config):
                return engines_config[idx - 1]
        for ec in engines_config:
            if (ec.get("instance_name") or "").strip() == name_or_idx:
                return ec
        return None

    def _get_engine_for_session(self, session_id: str) -> TTSEngine | None:
        """获取指定会话应使用的引擎（有绑定用绑定，否则用全局）"""
        if session_id and session_id in self._session_bindings:
            # 检查缓存的引擎实例
            if session_id in self._session_engines:
                return self._session_engines[session_id]
            # 创建引擎实例
            bound_name = self._session_bindings[session_id]
            ec = self._find_engine_config(bound_name)
            if ec:
                try:
                    engine = get_engine(ec, self.config)
                    self._session_engines[session_id] = engine
                    return engine
                except Exception as e:
                    logger.error(f"[UniversalTTS] 会话绑定引擎创建失败 ({bound_name}): {e}")
                    # 绑定失效，移除
                    del self._session_bindings[session_id]
                    self._save_bindings()
            else:
                # 绑定的引擎已不存在，移除
                logger.warning(f"[UniversalTTS] 会话绑定的引擎 '{bound_name}' 已不存在，已解除绑定")
                del self._session_bindings[session_id]
                self._save_bindings()
        return self._engine

    async def initialize(self):
        """插件初始化：创建引擎实例"""
        if not self.config.get("enable", True):
            logger.info("[UniversalTTS] 插件已禁用，跳过初始化")
            return

        # 加载会话绑定
        self._load_bindings()

        # 获取引擎配置列表
        engines_config: list[dict] = self.config.get("engines", [])
        if not engines_config:
            logger.warning("[UniversalTTS] 未配置任何 TTS 引擎，插件不会生效")
            return

        # 根据 active_engine 选择生效的全局引擎
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

        logger.info(f"[UniversalTTS] 已启用，全局引擎: {instance_name} (类型: {template_key})")
        if self._session_bindings:
            logger.info(f"[UniversalTTS] 已加载 {len(self._session_bindings)} 个会话绑定")

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

        # 获取当前会话应使用的引擎
        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if engine is None:
            return

        # 合成语音
        try:
            logger.debug(f"[UniversalTTS] 开始合成 (引擎: {engine.instance_name}): {full_text[:50]}...")
            audio_bytes, fmt = await engine.synthesize(full_text)
        except Exception as e:
            logger.error(
                f"[UniversalTTS] TTS 合成失败 (引擎: {engine.instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            return

        # 保存音频文件
        audio_path = self._temp_dir / f"tts_{uuid.uuid4().hex}.{fmt}"
        audio_path.write_bytes(audio_bytes)

        # 替换消息链为语音
        result.chain = [Comp.Record(file=str(audio_path), url=str(audio_path))]

    def _list_engines(self) -> list[dict]:
        """获取已配置的引擎列表"""
        return self.config.get("engines", []) or []

    def _format_instance(self, idx: int, ec: dict, session_id: str = "") -> str:
        """格式化引擎条目"""
        name = (ec.get("instance_name") or "").strip() or ec.get("__template_key", "unknown")
        ttype = ec.get("__template_key", "")
        markers = []
        if self._engine and self._engine.instance_name == name:
            markers.append("全局")
        if session_id and self._session_bindings.get(session_id) == name:
            markers.append("本会话")
        marker = f" ← {'|'.join(markers)}" if markers else ""
        return f"  {idx}. {name} ({ttype}){marker}"

    @filter.command("tts_switch")
    async def switch_engine(self, event: AstrMessageEvent):
        """[管理员] 切换全局 TTS 引擎。用法: /tts_switch <序号或实例名>"""
        if not _is_slash(event):
            return
        args = _get_cmd_args(event)
        engines_config = self._list_engines()
        session_id = _get_session_id(event)

        if not args:
            if not engines_config:
                yield event.plain_result("未配置任何 TTS 引擎")
                return
            lines = ["当前配置的 TTS 引擎："]
            for i, ec in enumerate(engines_config, 1):
                lines.append(self._format_instance(i, ec, session_id))
            lines.append("\n用法: /tts_switch <序号或实例名>")
            yield event.plain_result("\n".join(lines))
            return

        target_config = self._find_engine_config(args)
        if target_config is None:
            yield event.plain_result(
                f"未找到 '{args}' 对应的引擎。使用 /tts_switch 查看可用列表。"
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
        name = self._engine.instance_name
        # 同步更新配置中的 active_engine 字段
        self.config["active_engine"] = name
        logger.info(f"[UniversalTTS] 已切换全局引擎: {name}")
        yield event.plain_result(f"已切换全局 TTS 引擎为: {name}")

    @filter.command("tts_bind")
    async def bind_engine(self, event: AstrMessageEvent):
        """为当前会话绑定 TTS 引擎。用法: /tts_bind <序号或实例名>"""
        if not _is_slash(event):
            return
        args = _get_cmd_args(event)
        session_id = _get_session_id(event)

        if not session_id:
            yield event.plain_result("无法获取当前会话标识，绑定失败")
            return

        if not args:
            # 显示当前会话绑定状态
            bound = self._session_bindings.get(session_id)
            if bound:
                yield event.plain_result(
                    f"当前会话已绑定引擎: {bound}\n"
                    f"使用 /tts_unbind 解除绑定\n"
                    f"使用 /tts_bind <序号或实例名> 更换绑定"
                )
            else:
                yield event.plain_result(
                    f"当前会话未绑定引擎，使用全局默认\n"
                    f"使用 /tts_bind <序号或实例名> 绑定"
                )
            return

        target_config = self._find_engine_config(args)
        if target_config is None:
            yield event.plain_result(
                f"未找到 '{args}' 对应的引擎。使用 /tts_engines 查看可用列表。"
            )
            return

        instance_name = (target_config.get("instance_name") or "").strip() or target_config.get("__template_key", "")

        # 验证引擎可以创建
        try:
            engine = get_engine(target_config, self.config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return

        # 关闭旧的会话引擎缓存
        old_engine = self._session_engines.pop(session_id, None)
        if old_engine:
            await old_engine.close()

        # 保存绑定
        self._session_engines[session_id] = engine
        self._session_bindings[session_id] = instance_name
        self._save_bindings()

        logger.info(f"[UniversalTTS] 会话 {session_id[:20]}... 绑定引擎: {instance_name}")
        yield event.plain_result(f"已为当前会话绑定 TTS 引擎: {instance_name}")

    @filter.command("tts_unbind")
    async def unbind_engine(self, event: AstrMessageEvent):
        """解除当前会话的 TTS 引擎绑定。用法: /tts_unbind"""
        if not _is_slash(event):
            return
        session_id = _get_session_id(event)

        if not session_id:
            yield event.plain_result("无法获取当前会话标识")
            return

        if session_id not in self._session_bindings:
            yield event.plain_result("当前会话未绑定引擎，已使用全局默认")
            return

        old_name = self._session_bindings.pop(session_id)
        old_engine = self._session_engines.pop(session_id, None)
        if old_engine:
            await old_engine.close()
        self._save_bindings()

        logger.info(f"[UniversalTTS] 会话 {session_id[:20]}... 解除绑定 (原: {old_name})")
        yield event.plain_result(f"已解除当前会话的引擎绑定 (原: {old_name})，将使用全局默认")

    @filter.command("tts_test")
    async def test_tts(self, event: AstrMessageEvent):
        """测试 TTS 合成。用法: /tts_test <文本>"""
        if not _is_slash(event):
            return
        text = _get_cmd_args(event) or "你好，这是一条 TTS 测试消息。"

        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if not engine:
            yield event.plain_result("TTS 引擎未初始化，请检查插件配置")
            return

        try:
            audio_bytes, fmt = await engine.synthesize(text)
            audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
            audio_path.write_bytes(audio_bytes)
            yield event.chain_result(
                [Comp.Record(file=str(audio_path), url=str(audio_path))]
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 测试合成失败: {e}")
            yield event.plain_result(f"TTS 合成失败: {e}")

    @filter.command("tts_engines")
    async def list_engines(self, event: AstrMessageEvent):
        """列出所有已配置的 TTS 引擎实例"""
        if not _is_slash(event):
            return
        engines_config = self._list_engines()
        if not engines_config:
            yield event.plain_result("未配置任何 TTS 引擎")
            return
        session_id = _get_session_id(event)
        lines = ["已配置的 TTS 引擎实例："]
        for i, ec in enumerate(engines_config, 1):
            lines.append(self._format_instance(i, ec, session_id))
        lines.append("\n切换实例: /tts_bind <序号>")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_bindings")
    async def list_bindings(self, event: AstrMessageEvent):
        """[管理员] 查看所有会话绑定。用法: /tts_bindings"""
        if not _is_slash(event):
            return
        global_name = self._engine.instance_name if self._engine else "(未初始化)"
        lines = [f"全局默认引擎: {global_name}", ""]

        if not self._session_bindings:
            lines.append("暂无会话绑定，所有会话使用全局默认")
        else:
            lines.append(f"会话绑定 ({len(self._session_bindings)} 个)：")
            for i, (sid, name) in enumerate(self._session_bindings.items(), 1):
                short_sid = sid if len(sid) <= 30 else sid[:27] + "..."
                lines.append(f"  {i}. {short_sid} → {name}")

        lines.append("\n/tts_unbind_all 清除全部绑定")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_unbind_all")
    async def unbind_all(self, event: AstrMessageEvent):
        """[管理员] 清除所有会话绑定。用法: /tts_unbind_all"""
        if not _is_slash(event):
            return
        if not self._session_bindings:
            yield event.plain_result("当前没有任何会话绑定")
            return

        count = len(self._session_bindings)
        for engine in self._session_engines.values():
            await engine.close()
        self._session_engines.clear()
        self._session_bindings.clear()
        self._save_bindings()

        logger.info(f"[UniversalTTS] 已清除全部 {count} 个会话绑定")
        yield event.plain_result(f"已清除全部 {count} 个会话绑定，所有会话将使用全局默认")

    async def terminate(self):
        """插件卸载时释放引擎资源"""
        if self._engine:
            await self._engine.close()
        for engine in self._session_engines.values():
            await engine.close()
        self._session_engines.clear()
        logger.info("[UniversalTTS] 引擎资源已释放")
