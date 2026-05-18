"""通用 TTS 插件 - 通过钩子拦截消息，自行完成 TTS 合成"""

import inspect
import json
import random
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import get_engine, TTSEngine


DISABLED_ENGINE = "__disabled__"


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


def _get_event_user_ids(event: AstrMessageEvent) -> set[str]:
    """尽量从不同平台事件结构中提取发送者 ID。"""
    user_ids: set[str] = set()
    for obj in (event, getattr(event, "message_obj", None)):
        if obj is None:
            continue
        for attr in ("sender_id", "user_id", "userId", "qq", "wxid"):
            value = getattr(obj, attr, None)
            if value:
                user_ids.add(str(value))
        sender = getattr(obj, "sender", None)
        if isinstance(sender, dict):
            for key in ("user_id", "sender_id", "id", "qq", "wxid"):
                value = sender.get(key)
                if value:
                    user_ids.add(str(value))
        elif sender is not None:
            for attr in ("user_id", "sender_id", "id", "qq", "wxid"):
                value = getattr(sender, attr, None)
                if value:
                    user_ids.add(str(value))

    get_sender_id = getattr(event, "get_sender_id", None)
    if callable(get_sender_id):
        try:
            value = get_sender_id()
            if value:
                user_ids.add(str(value))
        except Exception:
            pass
    return user_ids


@register(
    "astrbot_plugin_universal_tts",
    "某不科学的高数",
    "通用 TTS 插件，支持多引擎切换，通过钩子拦截实现 TTS",
    "1.3.0",
)
class UniversalTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._engine: TTSEngine | None = None
        self._session_engines: dict[str, TTSEngine] = {}  # [已弃用] session_id -> engine，保持兼容但已不使用
        self._cached_engines: dict[str, TTSEngine] = {}  # instance_name -> engine（同名引擎复用实例）
        self._session_bindings: dict[str, str] = {}  # session_id -> instance_name
        self._runtime_enabled = True
        self._last_tts_text: dict[str, str] = {}  # session_id -> 最近一次 TTS 文本
        self._tts_audio_text: dict[str, str] = {}  # audio_path -> TTS 文本（用于引用还原）
        self._last_temp_cleanup_at = 0.0
        self._temp_dir = Path("data") / "temp" / "universal_tts"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = Path("data") / "plugin_data" / "astrbot_plugin_universal_tts"
        self._bindings_file = self._data_dir / "session_bindings.json"
        self._runtime_state_file = self._data_dir / "runtime_state.json"
        self._data_dir.mkdir(parents=True, exist_ok=True)

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

    def _load_runtime_state(self):
        """加载运行时开关状态。"""
        if not self._runtime_state_file.exists():
            return
        try:
            data = json.loads(self._runtime_state_file.read_text(encoding="utf-8"))
            self._runtime_enabled = bool(data.get("runtime_enabled", True))
        except Exception as e:
            logger.warning(f"[UniversalTTS] 加载运行状态失败，使用默认开启状态: {e}")
            self._runtime_enabled = True

    def _save_runtime_state(self):
        """持久化运行时开关状态。"""
        try:
            self._runtime_state_file.write_text(
                json.dumps({"runtime_enabled": self._runtime_enabled}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 保存运行状态失败: {e}")

    async def _has_admin_permission(self, event: AstrMessageEvent) -> bool:
        """检查是否允许执行全局管理命令。"""
        if not self.config.get("admin_only_commands", True):
            return True

        admin_ids = {str(item).strip() for item in self.config.get("admin_user_ids", []) if str(item).strip()}
        if admin_ids and _get_event_user_ids(event) & admin_ids:
            return True

        for method_name in ("is_admin", "is_at_admin", "is_msg_admin"):
            method = getattr(event, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    return True
            except Exception as e:
                logger.debug(f"[UniversalTTS] 管理员权限检查 {method_name} 失败: {e}")

        return False

    async def _ensure_admin_permission(self, event: AstrMessageEvent) -> bool:
        if await self._has_admin_permission(event):
            return True
        logger.warning("[UniversalTTS] 已拒绝非管理员执行 TTS 管理命令")
        return False

    def _cleanup_temp_files(self, force: bool = False):
        """清理插件生成的过期临时音频文件。"""
        now = time.time()
        if not force and now - self._last_temp_cleanup_at < 300:
            return
        self._last_temp_cleanup_at = now

        ttl_hours = self.config.get("temp_file_ttl_hours", 72)
        max_count = self.config.get("temp_file_max_count", 1000)
        try:
            ttl_hours = max(0, int(ttl_hours))
        except (TypeError, ValueError):
            ttl_hours = 72
        try:
            max_count = max(0, int(max_count))
        except (TypeError, ValueError):
            max_count = 1000

        files = []
        for path in self._temp_dir.iterdir():
            if not path.is_file() or not path.name.startswith(("tts_", "tts_test_")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((path, stat.st_mtime))

        removed = 0
        expire_before = now - ttl_hours * 3600 if ttl_hours > 0 else None
        remaining = []
        for path, mtime in files:
            if expire_before is not None and mtime < expire_before:
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning(f"[UniversalTTS] 删除过期临时文件失败 {path}: {e}")
            else:
                remaining.append((path, mtime))

        if max_count > 0 and len(remaining) > max_count:
            remaining.sort(key=lambda item: item[1])
            for path, _ in remaining[: len(remaining) - max_count]:
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning(f"[UniversalTTS] 删除超量临时文件失败 {path}: {e}")

        if removed:
            logger.info(f"[UniversalTTS] 已清理 {removed} 个临时音频文件")

    def _find_engine_config(self, name_or_idx: str) -> dict | None:
        """按序号或实例名查找引擎配置"""
        if name_or_idx == "0":
            return None
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
            bound_name = self._session_bindings[session_id]
            if bound_name == DISABLED_ENGINE:
                return None
            # 检查同名引擎是否已缓存，复用实例
            if bound_name in self._cached_engines:
                return self._cached_engines[bound_name]
            # 创建新引擎实例并缓存
            ec = self._find_engine_config(bound_name)
            if ec:
                try:
                    engine = get_engine(ec, self.config)
                    self._cached_engines[bound_name] = engine
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
        self._load_runtime_state()
        self._cleanup_temp_files(force=True)

        # 获取引擎配置列表
        engines_config: list[dict] = self.config.get("engines", [])
        if not engines_config:
            logger.warning("[UniversalTTS] 未配置任何 TTS 引擎，插件不会生效")
            return

        # 根据 active_engine 选择生效的全局引擎
        active_engine_name = self.config.get("active_engine", "").strip()
        engine_config = None

        if active_engine_name == DISABLED_ENGINE:
            logger.info("[UniversalTTS] 已启用，全局默认 TTS 已关闭")
            if self._session_bindings:
                logger.info(f"[UniversalTTS] 已加载 {len(self._session_bindings)} 个会话绑定")
            return

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
        if not self._runtime_enabled:
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

        # 字数上限检查
        if self.config.get("tts_text_limit_enable", False):
            limit = self.config.get("tts_text_limit", 50)
            if len(full_text) > limit:
                logger.debug(
                    f"[UniversalTTS] 文本字数 ({len(full_text)}) 超过上限 ({limit})，跳过 TTS"
                )
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
        self._cleanup_temp_files()

        # 替换消息链中的文本部分为语音，非文本组件保持原有顺序。
        audio_path_str = str(audio_path)
        self._tts_audio_text[audio_path_str] = full_text
        self._last_tts_text[session_id] = full_text
        logger.info(f"[UniversalTTS] 发送语音 ({engine.instance_name}): {full_text[:80]}")
        record = Comp.Record(file=audio_path_str, url=audio_path_str)
        new_chain = []
        inserted_record = False
        for component in result.chain:
            if isinstance(component, Comp.Plain):
                if component.text and not inserted_record:
                    new_chain.append(record)
                    inserted_record = True
                continue
            new_chain.append(component)
        result.chain = new_chain

    @filter.on_waiting_llm_request()
    async def tts_reply_fix(self, event: AstrMessageEvent):
        """修复引用语音消息时因 NapCat 临时文件已被清除而导致 'not a valid file' 报错的问题。
        在 LLM 请求构建之前，将 Reply 链中的 Record 替换为缓存的文本。"""
        session_id = _get_session_id(event)
        session_last_text = self._last_tts_text.get(session_id, "")

        for comp in event.get_messages():
            if not isinstance(comp, Comp.Reply) or not comp.chain:
                continue
            new_chain = []
            modified = False
            for reply_comp in comp.chain:
                if isinstance(reply_comp, Comp.Record):
                    # 优先按文件路径匹配，其次使用会话最近一次文本
                    cached = None
                    file_path = getattr(reply_comp, "file", "") or getattr(reply_comp, "url", "")
                    if file_path:
                        cached = self._tts_audio_text.get(file_path)
                    if not cached and session_last_text:
                        cached = session_last_text

                    if cached:
                        new_chain.append(Comp.Plain(cached))
                        logger.debug(
                            f"[UniversalTTS] 已将引用语音替换为缓存文本: {cached[:30]}..."
                        )
                    else:
                        new_chain.append(Comp.Plain("[语音消息]"))
                        logger.debug(
                            "[UniversalTTS] 未找到缓存文本，使用占位符替代引用语音"
                        )
                    modified = True
                else:
                    new_chain.append(reply_comp)
            if modified:
                comp.chain = new_chain

    def _list_engines(self) -> list[dict]:
        """获取已配置的引擎列表"""
        return self.config.get("engines", []) or []

    def _format_binding_name(self, name: str) -> str:
        """格式化绑定名称。"""
        return "禁用 TTS" if name == DISABLED_ENGINE else name

    def _global_engine_name(self) -> str:
        if self.config.get("active_engine", "").strip() == DISABLED_ENGINE:
            return "关闭"
        return self._engine.instance_name if self._engine else "(未初始化)"

    def _session_tts_status(self, session_id: str) -> str:
        binding = self._session_bindings.get(session_id)
        if binding == DISABLED_ENGINE:
            return "当前会话已禁用 TTS"
        if binding:
            return f"当前会话绑定引擎: {binding}"
        global_name = self._global_engine_name()
        return f"当前会话使用全局默认: {global_name}"

    def _format_instance(self, idx: int, ec: dict, session_id: str = "") -> str:
        """格式化引擎条目"""
        name = (ec.get("instance_name") or "").strip() or ec.get("__template_key", "unknown")
        ttype = ec.get("__template_key", "")
        markers = []
        if self._engine and self.config.get("active_engine", "").strip() != DISABLED_ENGINE and self._engine.instance_name == name:
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
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        args = _get_cmd_args(event)
        engines_config = self._list_engines()
        session_id = _get_session_id(event)

        if not args:
            if not engines_config:
                yield event.plain_result("未配置任何 TTS 引擎")
                return
            lines = [
                f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
                f"全局默认引擎: {self._global_engine_name()}",
                "",
                "当前配置的 TTS 引擎：",
                "  0. 关闭全局默认 TTS",
            ]
            for i, ec in enumerate(engines_config, 1):
                lines.append(self._format_instance(i, ec, session_id))
            lines.append("\n用法: /tts_switch <序号或实例名>，/tts_switch 0 关闭全局默认 TTS")
            yield event.plain_result("\n".join(lines))
            return

        if args == "0":
            if self._engine:
                await self._engine.close()
            self._engine = None
            self.config["active_engine"] = DISABLED_ENGINE
            logger.info("[UniversalTTS] 已关闭全局默认 TTS")
            yield event.plain_result("已关闭全局默认 TTS。已绑定具体引擎的会话不受影响，使用 /tts_off 可暂停全部会话。")
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
            if bound == DISABLED_ENGINE:
                yield event.plain_result(
                    "当前会话已禁用 TTS\n"
                    "使用 /tts_unbind 回退全局默认\n"
                    "使用 /tts_bind <序号或实例名> 绑定具体引擎"
                )
            elif bound:
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

        if args == "0":
            self._session_bindings[session_id] = DISABLED_ENGINE
            self._save_bindings()
            logger.info(f"[UniversalTTS] 会话 {session_id[:20]}... 已禁用 TTS")
            yield event.plain_result("已为当前会话禁用 TTS。使用 /tts_unbind 可回退全局默认。")
            return

        target_config = self._find_engine_config(args)
        if target_config is None:
            yield event.plain_result(
                f"未找到 '{args}' 对应的引擎。使用 /tts_engines 查看可用列表。"
            )
            return

        instance_name = (target_config.get("instance_name") or "").strip() or target_config.get("__template_key", "")

        # 验证引擎配置有效，成功后直接缓存实例，避免重复构造。
        try:
            engine = get_engine(target_config, self.config)
        except Exception as e:
            yield event.plain_result(f"创建引擎失败: {e}")
            return
        old_engine = self._cached_engines.get(instance_name)
        if old_engine and old_engine is not engine:
            await old_engine.close()
        self._cached_engines[instance_name] = engine

        # 保存绑定（引擎实例在首次使用时从 _cached_engines 复用）
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
        # 不再关闭引擎实例，其他会话可能仍在复用
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
            yield event.plain_result("当前会话没有可用 TTS 引擎，请检查全局默认或会话绑定状态")
            return

        try:
            audio_bytes, fmt = await engine.synthesize(text)
            audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
            audio_path.write_bytes(audio_bytes)
            self._cleanup_temp_files()
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
        session_id = _get_session_id(event)
        lines = [
            f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
            f"全局默认引擎: {self._global_engine_name()}",
            self._session_tts_status(session_id),
            "",
            "可用 TTS 引擎：",
            "  0. 关闭当前会话 TTS",
        ]
        for i, ec in enumerate(engines_config, 1):
            lines.append(self._format_instance(i, ec, session_id))
        if not engines_config:
            lines.append("  (未配置任何 TTS 引擎)")
        lines.append("\n当前会话关闭: /tts_bind 0")
        lines.append("当前会话绑定: /tts_bind <序号>")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_bindings")
    async def list_bindings(self, event: AstrMessageEvent):
        """[管理员] 查看所有会话绑定。用法: /tts_bindings"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        lines = [
            f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
            f"全局默认引擎: {self._global_engine_name()}",
            "",
        ]

        if not self._session_bindings:
            lines.append("暂无会话绑定，所有会话使用全局默认")
        else:
            lines.append(f"会话绑定 ({len(self._session_bindings)} 个)：")
            for i, (sid, name) in enumerate(self._session_bindings.items(), 1):
                short_sid = sid if len(sid) <= 30 else sid[:27] + "..."
                lines.append(f"  {i}. {short_sid} → {self._format_binding_name(name)}")

        lines.append("\n/tts_unbind_all 清除全部绑定")
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_unbind_all")
    async def unbind_all(self, event: AstrMessageEvent):
        """[管理员] 清除所有会话绑定。用法: /tts_unbind_all"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        if not self._session_bindings:
            yield event.plain_result("当前没有任何会话绑定")
            return

        count = len(self._session_bindings)
        # 关闭所有被会话绑定了的缓存引擎
        for engine in self._cached_engines.values():
            await engine.close()
        self._cached_engines.clear()
        self._session_bindings.clear()
        self._save_bindings()

        logger.info(f"[UniversalTTS] 已清除全部 {count} 个会话绑定")
        yield event.plain_result(f"已清除全部 {count} 个会话绑定，所有会话将使用全局默认")

    @filter.command("tts_off")
    async def disable_runtime(self, event: AstrMessageEvent):
        """[管理员] 暂停所有会话的自动 TTS。用法: /tts_off"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self._runtime_enabled = False
        self._save_runtime_state()
        logger.info("[UniversalTTS] 已暂停全部会话的自动 TTS")
        yield event.plain_result("已暂停全部会话的自动 TTS。会话绑定不会被修改，使用 /tts_on 恢复。")

    @filter.command("tts_on")
    async def enable_runtime(self, event: AstrMessageEvent):
        """[管理员] 恢复所有会话的自动 TTS。用法: /tts_on"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self._runtime_enabled = True
        self._save_runtime_state()
        logger.info("[UniversalTTS] 已恢复自动 TTS")
        yield event.plain_result("已恢复自动 TTS。全局默认和会话绑定将按原状态生效。")

    @filter.command("tts_status")
    async def tts_status(self, event: AstrMessageEvent):
        """查看 TTS 开关和当前会话状态。用法: /tts_status"""
        if not _is_slash(event):
            return
        session_id = _get_session_id(event)
        lines = [
            f"TTS 总开关: {'开启' if self._runtime_enabled else '关闭'}",
            f"全局默认引擎: {self._global_engine_name()}",
            self._session_tts_status(session_id),
            "",
            "全局暂停: /tts_off",
            "全局恢复: /tts_on",
            "关闭当前会话: /tts_bind 0",
            "关闭全局默认: /tts_switch 0",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("tts_limit_on")
    async def enable_limit(self, event: AstrMessageEvent):
        """开启 TTS 字数上限。用法: /tts_limit_on"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self.config["tts_text_limit_enable"] = True
        limit = self.config.get("tts_text_limit", 50)
        yield event.plain_result(f"已开启 TTS 字数上限，当前上限: {limit} 字符")

    @filter.command("tts_limit_off")
    async def disable_limit(self, event: AstrMessageEvent):
        """关闭 TTS 字数上限。用法: /tts_limit_off"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self.config["tts_text_limit_enable"] = False
        yield event.plain_result("已关闭 TTS 字数上限")

    @filter.command("tts_limit_set")
    async def set_limit(self, event: AstrMessageEvent):
        """设置 TTS 字数上限。用法: /tts_limit_set <数字>"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        args = _get_cmd_args(event)
        if not args:
            current_limit = self.config.get("tts_text_limit", 50)
            current_enable = self.config.get("tts_text_limit_enable", False)
            status = "已开启" if current_enable else "已关闭"
            yield event.plain_result(
                f"当前 TTS 字数上限: {current_limit} 字符 ({status})\n"
                f"用法: /tts_limit_set <数字>"
            )
            return
        try:
            limit = int(args)
            if limit <= 0:
                yield event.plain_result("字数上限必须大于 0")
                return
        except ValueError:
            yield event.plain_result(f"无效的数字: {args}")
            return
        self.config["tts_text_limit"] = limit
        yield event.plain_result(f"已设置 TTS 字数上限为: {limit} 字符")

    async def terminate(self):
        """插件卸载时释放引擎资源"""
        if self._engine:
            await self._engine.close()
        for engine in self._cached_engines.values():
            await engine.close()
        self._cached_engines.clear()
        logger.info("[UniversalTTS] 引擎资源已释放")
