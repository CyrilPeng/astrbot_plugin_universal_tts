"""通用 TTS 插件 - 通过钩子拦截消息，自行完成 TTS 合成"""

import asyncio
import base64
import hashlib
import inspect
import json
import random
import re
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

from .engines import get_engine, TTSEngine

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except Exception:
    SessionServiceManager = None

# WebUI（插件可视化视图）后端 API 依赖 AstrBot >= 4.25.0。
# 导入失败时静默降级：旧版本会忽略 views/ 目录，插件其余功能完全不受影响。
try:
    from astrbot.api.web import error_response, json_response, request
except Exception:  # pragma: no cover - 旧版本 AstrBot
    error_response = json_response = request = None

# file_response 单独导入：个别版本可能还没有它，不能因为它在而拖垮上面整组 API
try:
    from astrbot.api.web import file_response
except Exception:  # pragma: no cover - 旧版本 AstrBot
    file_response = None


PLUGIN_NAME = "astrbot_plugin_universal_tts"

# 命中这些名字的字段在 WebUI 列表中会被脱敏；前端留空即表示"沿用原值"。
_SENSITIVE_FIELD_RE = re.compile(
    r"(^|_)(api_key|access_token|subscription_key|token|secret|password)($|_)",
    re.IGNORECASE,
)


DISABLED_ENGINE = "__disabled__"

# ---------------------------------------------------------------------------
# WebUI 试听 / 样本管理
# ---------------------------------------------------------------------------

# 试听用的固定短句：保持较短，避免 base64 传输过大
PREVIEW_TEXT = "你好，这是一段音色试听。"
# 试听音频上限，超过则拒绝回传（base64 会再膨胀 4/3）
PREVIEW_MAX_BYTES = 4 * 1024 * 1024
# 单次试听的硬超时：超时即判失败，避免请求无限挂起被网关掐断
PREVIEW_TIMEOUT = 60
# 试听任务结果的保留时长与数量上限
PREVIEW_JOB_TTL = 300
PREVIEW_MAX_JOBS = 20
# 单个样本文件上限
SAMPLE_MAX_BYTES = 20 * 1024 * 1024
# 允许的样本扩展名（与 _conf_schema.json 的 file_types 保持一致）
SAMPLE_EXTS = {".mp3", ".wav"}
# 样本文件相对路径前缀（与 AstrBot file 字段的落盘规则一致）
SAMPLE_REL_DIR = "files/voice_clone_samples"

_AUDIO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "pcm16": "audio/wav",
}
# 浏览器可直接播放的格式
_PLAYABLE_FORMATS = {"wav", "mp3", "mpeg", "ogg", "opus", "flac", "aac"}


# ---------------------------------------------------------------------------
# 指令说明（唯一数据源）
#
# WebUI 的「指令与帮助」页、聊天里的 /tts_help、以及 metadata.yaml 的 help 字段
# 都从这里生成。新增指令时必须同步登记，否则单测会直接失败，避免文档悄悄过期。
# ---------------------------------------------------------------------------
COMMAND_SPECS: list[dict] = [
    {
        "name": "tts_help",
        "usage": "/tts_help",
        "summary": "查看指令帮助与当前运行状态",
        "admin": False,
        "examples": ["/tts_help"],
        "notes": ["WebUI 里「指令与帮助」页有同样内容，并会显示更完整的实时状态"],
    },
    {
        "name": "tts_status",
        "usage": "/tts_status",
        "summary": "查看总开关、全局默认引擎与当前会话状态",
        "admin": False,
        "examples": ["/tts_status"],
        "notes": [],
    },
    {
        "name": "tts_engines",
        "usage": "/tts_engines",
        "summary": "列出全部引擎实例与编号，并标出当前会话用的是哪个",
        "admin": False,
        "examples": ["/tts_engines"],
        "notes": ["编号从 1 开始，供 /tts_switch 与 /tts_bind 使用"],
    },
    {
        "name": "tts_bind",
        "usage": "/tts_bind [序号或实例名]",
        "summary": "让当前会话固定使用某个引擎",
        "admin": False,
        "examples": ["/tts_bind", "/tts_bind 2", "/tts_bind 紫苏九月", "/tts_bind 0"],
        "notes": [
            "不带参数：只查看当前会话的绑定状态",
            "填 0：本会话不再合成语音",
            "用 /tts_unbind 恢复为跟随全局默认",
        ],
    },
    {
        "name": "tts_unbind",
        "usage": "/tts_unbind",
        "summary": "解除当前会话的绑定，恢复跟随全局默认引擎",
        "admin": False,
        "examples": ["/tts_unbind"],
        "notes": [],
    },
    {
        "name": "tts_test",
        "usage": "/tts_test [文本]",
        "summary": "立即合成一条语音发到当前会话，用于验证配置是否可用",
        "admin": False,
        "examples": ["/tts_test", "/tts_test 你好，测试一下音色"],
        "notes": [
            "不填文本时使用「你好，这是一条 TTS 测试消息。」",
            "需要 AstrBot 全局 TTS 开关处于开启状态",
        ],
    },
    {
        "name": "tts_switch",
        "usage": "/tts_switch [序号或实例名]",
        "summary": "切换全局默认引擎",
        "admin": True,
        "examples": ["/tts_switch", "/tts_switch 1", "/tts_switch 0"],
        "notes": [
            "不带参数：列出全部引擎及当前状态",
            "填 0：关闭全局默认 TTS，已单独绑定的会话不受影响",
        ],
    },
    {
        "name": "tts_bindings",
        "usage": "/tts_bindings",
        "summary": "列出所有会话的引擎绑定",
        "admin": True,
        "examples": ["/tts_bindings"],
        "notes": ["WebUI 的「会话绑定」页可以逐条改绑或解绑"],
    },
    {
        "name": "tts_unbind_all",
        "usage": "/tts_unbind_all",
        "summary": "清除全部会话绑定",
        "admin": True,
        "examples": ["/tts_unbind_all"],
        "notes": ["清除后所有会话都跟随全局默认引擎"],
    },
    {
        "name": "tts_off",
        "usage": "/tts_off",
        "summary": "暂停所有会话的自动语音合成",
        "admin": True,
        "examples": ["/tts_off"],
        "notes": ["不会改动会话绑定，用 /tts_on 按原样恢复"],
    },
    {
        "name": "tts_on",
        "usage": "/tts_on",
        "summary": "恢复所有会话的自动语音合成",
        "admin": True,
        "examples": ["/tts_on"],
        "notes": [],
    },
    {
        "name": "tts_limit_on",
        "usage": "/tts_limit_on",
        "summary": "开启文本长度上限，超长回复不再合成语音",
        "admin": True,
        "examples": ["/tts_limit_on"],
        "notes": ["上限值用 /tts_limit_set 调整，也可在 WebUI「全局设置」页修改"],
    },
    {
        "name": "tts_limit_off",
        "usage": "/tts_limit_off",
        "summary": "关闭文本长度上限",
        "admin": True,
        "examples": ["/tts_limit_off"],
        "notes": [],
    },
    {
        "name": "tts_limit_set",
        "usage": "/tts_limit_set [数字]",
        "summary": "设置文本长度上限",
        "admin": True,
        "examples": ["/tts_limit_set", "/tts_limit_set 80"],
        "notes": [
            "不带参数：显示当前上限与开关状态",
            "必须是大于 0 的整数",
        ],
    },
]


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


def _format_session_id(session_id: str) -> str:
    """将 UMO 格式化为稳定、可读的会话标识。"""
    parts = session_id.split(":")
    if len(parts) >= 3:
        message_type = parts[-2]
        target_id = parts[-1]
        type_map = {
            "GroupMessage": "群聊",
            "FriendMessage": "私聊",
        }
        label = type_map.get(message_type, message_type)
        return f"{label}:{target_id}"
    return session_id if len(session_id) <= 48 else session_id[:45] + "..."


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


def _read_plugin_version(fallback: str = "0.0.0") -> str:
    """从 metadata.yaml 读取版本号，避免 @register 中的版本与实际发布脱节。"""
    try:
        import yaml

        meta_path = Path(__file__).with_name("metadata.yaml")
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        version = str(data.get("version") or "").strip().lstrip("vV")
        if version:
            return version
    except Exception as e:
        logger.debug(f"[UniversalTTS] 读取 metadata.yaml 版本号失败，回退到 {fallback}: {e}")
    return fallback


@register(
    "astrbot_plugin_universal_tts",
    "某不科学的高数",
    "通用 TTS 插件，支持多引擎切换，通过钩子拦截实现 TTS",
    _read_plugin_version("1.4.2"),
)
class UniversalTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._engine: TTSEngine | None = None
        self._cached_engines: dict[str, TTSEngine] = {}  # instance_name -> engine（同名引擎复用实例）
        self._session_bindings: dict[str, str] = {}  # session_id -> instance_name
        self._runtime_enabled = True
        self._last_tts_text: dict[str, str] = {}  # session_id -> 最近一次 TTS 文本
        self._tts_audio_text: dict[str, str] = {}  # audio_path -> TTS 文本（用于引用还原）
        self._engine_stats: dict[str, dict] = {}  # instance_name -> 合成统计（内存态，重启清零）
        self._warned_skip_patterns: set[str] = set()  # 已告警过的无效跳过正则
        self._session_activity: dict[str, dict] = {}  # session_id -> 近期活跃（只读展示用，不参与引擎选择）
        self._last_activity_save_at = 0.0
        self._last_temp_cleanup_at = 0.0
        self._temp_dir = Path("data") / "temp" / "universal_tts"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = Path("data") / "plugin_data" / "astrbot_plugin_universal_tts"
        self._bindings_file = self._data_dir / "session_bindings.json"
        self._activity_file = self._data_dir / "session_activity.json"
        self._runtime_state_file = self._data_dir / "runtime_state.json"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # WebUI（插件可视化视图）相关
        self._context = context
        self._templates_cache: dict | None = None
        self._schema_cache: dict | None = None
        self._preview_jobs: dict[str, dict] = {}
        self._register_web_apis()

    def _load_bindings(self):
        """从文件加载会话绑定"""
        if self._bindings_file.exists():
            try:
                self._session_bindings = json.loads(self._bindings_file.read_text(encoding="utf-8"))
            except Exception:
                self._session_bindings = {}

    # ------------------------------------------------------------------
    # 会话活跃记录（只读展示用，不参与引擎选择）
    # ------------------------------------------------------------------

    #: 活跃记录上限与保留期：超过即按 last_at 最旧淘汰 / 启动加载时丢弃。
    ACTIVITY_MAX_ENTRIES = 200
    ACTIVITY_RETENTION_SECONDS = 30 * 24 * 3600
    ACTIVITY_SAVE_INTERVAL = 60.0

    def _load_activity(self):
        """加载会话活跃记录，丢弃超期条目。"""
        if not self._activity_file.exists():
            return
        try:
            raw = json.loads(self._activity_file.read_text(encoding="utf-8"))
        except Exception:
            self._session_activity = {}
            return
        if not isinstance(raw, dict):
            self._session_activity = {}
            return
        now = time.time()
        activity: dict[str, dict] = {}
        for sid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            last_at = entry.get("last_at", 0)
            try:
                last_at = float(last_at)
            except (TypeError, ValueError):
                continue
            if now - last_at > self.ACTIVITY_RETENTION_SECONDS:
                continue
            activity[str(sid)] = {
                "last_at": last_at,
                "engine": str(entry.get("engine") or ""),
                "count": max(1, int(entry.get("count", 1) or 1)),
            }
        # 加载时即整体截断，防止文件被外部改大后内存膨胀。
        if len(activity) > self.ACTIVITY_MAX_ENTRIES:
            activity = dict(
                sorted(activity.items(), key=lambda kv: kv[1]["last_at"], reverse=True)[
                    : self.ACTIVITY_MAX_ENTRIES
                ]
            )
        self._session_activity = activity

    def _save_activity(self, force: bool = False):
        """持久化会话活跃记录（节流保存）。"""
        now = time.time()
        if not force and now - self._last_activity_save_at < self.ACTIVITY_SAVE_INTERVAL:
            return
        self._last_activity_save_at = now
        try:
            self._activity_file.write_text(
                json.dumps(self._session_activity, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 保存会话活跃记录失败: {e}")

    def _record_activity(self, session_id: str, engine_name: str):
        """记录一次成功的合成：更新时间、引擎与累计次数。"""
        if not session_id:
            return
        entry = self._session_activity.get(session_id)
        if entry is None:
            entry = {"last_at": 0.0, "engine": "", "count": 0}
            self._session_activity[session_id] = entry
        entry["last_at"] = time.time()
        entry["engine"] = engine_name
        entry["count"] = int(entry.get("count", 0) or 0) + 1
        if len(self._session_activity) > self.ACTIVITY_MAX_ENTRIES:
            oldest = min(
                self._session_activity.items(), key=lambda kv: kv[1].get("last_at", 0)
            )[0]
            self._session_activity.pop(oldest, None)
        self._save_activity()

    def _activity_entries(self) -> list[dict]:
        """构造返回给 WebUI 的活跃列表（含已绑定会话，前端按筛选视图取用），按最后活跃倒序。"""
        entries: list[dict] = []
        for session_id, entry in self._session_activity.items():
            try:
                display = _format_session_id(session_id)
            except Exception:
                display = session_id
            entries.append(
                {
                    "session_id": session_id,
                    "display": display,
                    "engine": str(entry.get("engine") or ""),
                    "last_at": float(entry.get("last_at", 0) or 0),
                    "count": int(entry.get("count", 0) or 0),
                    # 当前绑定状态：""=跟随全局，DISABLED_ENGINE=禁音，其余为引擎名
                    "binding": str(self._session_bindings.get(session_id, "") or ""),
                }
            )
        entries.sort(key=lambda e: e["last_at"], reverse=True)
        return entries

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

    async def _persist_config(self) -> None:
        """把运行时对配置的修改写回配置文件。

        AstrBotConfig 继承自 dict 且未重写 __setitem__，赋值只改内存，
        必须显式保存，否则 /tts_switch、/tts_limit_* 的改动重启后会丢失。
        """
        try:
            save_async = getattr(self.config, "save_config_async", None)
            if callable(save_async):
                await save_async()
                return
            save = getattr(self.config, "save_config", None)
            if callable(save):
                await asyncio.to_thread(save)
        except Exception as e:
            logger.error(f"[UniversalTTS] 保存插件配置失败: {e}")

    def _prune_audio_text_cache(self, max_entries: int = 500) -> None:
        """清理音频→文本缓存：丢弃文件已不存在的条目，并把总量限制在 max_entries 内。"""
        if not self._tts_audio_text:
            return
        for path_str in list(self._tts_audio_text):
            try:
                if not Path(path_str).exists():
                    self._tts_audio_text.pop(path_str, None)
            except OSError:
                self._tts_audio_text.pop(path_str, None)
        overflow = len(self._tts_audio_text) - max_entries
        if overflow > 0:
            for path_str in list(self._tts_audio_text)[:overflow]:
                self._tts_audio_text.pop(path_str, None)
            logger.debug(f"[UniversalTTS] 音频文本缓存超限，已清理 {overflow} 条")

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

    async def _is_astrbot_tts_enabled(self, event: AstrMessageEvent) -> bool:
        """对齐 AstrBot 内置 TTS 开关：全局和会话均启用时才接管。"""
        try:
            astrbot_config = (
                getattr(self.context, "astrbot_config", None)
                or getattr(self.context, "_config", None)
            )
            tts_settings = astrbot_config.get("provider_tts_settings", {}) if astrbot_config else {}
            if not bool(tts_settings.get("enable", False)):
                return False
        except Exception as e:
            logger.debug(f"[UniversalTTS] 读取 AstrBot TTS 全局开关失败，跳过接管: {e}")
            return False

        if SessionServiceManager is not None:
            try:
                return await SessionServiceManager.should_process_tts_request(event)
            except Exception as e:
                logger.debug(f"[UniversalTTS] 读取 AstrBot TTS 会话开关失败，跳过接管: {e}")
                return False

        return True

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

        self._prune_audio_text_cache()

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
        self._load_activity()
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

    @filter.on_decorating_result(priority=0)
    async def tts_hook(self, event: AstrMessageEvent):
        """钩子：在消息发送前将文本转为语音"""
        if not self.config.get("enable", True):
            return
        if not self._runtime_enabled:
            return
        if not await self._is_astrbot_tts_enabled(event):
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

        # 智能跳过：代码块、纯链接、自定义正则
        skip_reason = self._should_skip_tts(full_text)
        if skip_reason:
            logger.debug(f"[UniversalTTS] 命中跳过规则 ({skip_reason})，跳过 TTS")
            return

        # 获取当前会话应使用的引擎
        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if engine is None:
            return

        # 合成语音
        try:
            logger.debug(f"[UniversalTTS] 开始合成 (引擎: {engine.instance_name}): {full_text[:50]}...")
            audio_bytes, fmt = await self._synthesize_with_stats(engine, full_text)
        except Exception as e:
            logger.error(
                f"[UniversalTTS] TTS 合成失败 (引擎: {engine.instance_name}): "
                f"{type(e).__name__}: {e}"
            )
            return

        # 保存音频文件（放线程池，避免阻塞事件循环）
        audio_path = self._temp_dir / f"tts_{uuid.uuid4().hex}.{fmt}"
        await asyncio.to_thread(audio_path.write_bytes, audio_bytes)
        self._cleanup_temp_files()

        # 替换消息链中的文本部分为语音，非文本组件保持原有顺序。
        audio_path_str = str(audio_path)
        self._tts_audio_text[audio_path_str] = full_text
        self._last_tts_text[session_id] = full_text
        # 记录活跃：成功合成才写，跳过/失败/暂停的不记。
        self._record_activity(session_id, engine.instance_name)
        logger.info(f"[UniversalTTS] 发送语音 ({engine.instance_name}): {full_text[:80]}")
        record = Comp.Record(file=audio_path_str, url=audio_path_str)
        original_chain = list(result.chain)
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
        self._install_send_fallback(event, result, original_chain)

    def _install_send_fallback(
        self, event: AstrMessageEvent, result, original_chain: list
    ) -> None:
        """包装 event.send：语音发送失败时改发原始文本。

        AstrBot 的 respond 阶段用 try/except 包住 event.send 且只写日志，
        发送失败时用户侧完全静默。这里在事件实例上包一层，失败时补发文本，
        成功时行为与原来完全一致。
        """
        if not self.config.get("fallback_to_text_on_send_error", True):
            return

        original_send = getattr(event, "send", None)
        if not callable(original_send):
            return
        if getattr(event, "_universal_tts_send_wrapped", False):
            return

        try:
            fallback_chain = result.derive(list(original_chain))
        except Exception as e:
            logger.debug(f"[UniversalTTS] 构造文本回退链失败，跳过回退: {e}")
            return

        state = {"used": False}

        async def _send_with_fallback(chain):
            try:
                return await original_send(chain)
            except Exception as e:
                logger.error(
                    f"[UniversalTTS] 语音发送失败，回退为文本: {type(e).__name__}: {e}"
                )
                if state["used"]:
                    raise
                state["used"] = True
                try:
                    return await original_send(fallback_chain)
                except Exception as e2:
                    logger.error(
                        f"[UniversalTTS] 文本回退同样失败: {type(e2).__name__}: {e2}"
                    )
                    raise

        try:
            event.send = _send_with_fallback
            event._universal_tts_send_wrapped = True
        except Exception as e:
            logger.debug(f"[UniversalTTS] 安装发送回退失败，已忽略: {e}")

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

    # ------------------------------------------------------------------
    # 合成统计与智能跳过
    # ------------------------------------------------------------------

    def _engine_stats_bucket(self, name: str) -> dict:
        return self._engine_stats.setdefault(
            name,
            {
                "success": 0,
                "fail": 0,
                "total_ms": 0,
                "max_ms": 0,
                "last_error": "",
                "last_error_at": 0.0,
                "last_success_at": 0.0,
            },
        )

    async def _synthesize_with_stats(self, engine, text: str):
        """带统计的合成封装：成功/失败/耗时统一记录，异常原样抛出。"""
        name = getattr(engine, "instance_name", "") or "(未知)"
        bucket = self._engine_stats_bucket(name)
        start = time.monotonic()
        try:
            audio, audio_format = await engine.synthesize(text)
        except Exception as e:
            bucket["fail"] += 1
            bucket["last_error"] = f"{type(e).__name__}: {e}"[:200]
            bucket["last_error_at"] = time.time()
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        bucket["success"] += 1
        bucket["total_ms"] += elapsed_ms
        bucket["max_ms"] = max(bucket["max_ms"], elapsed_ms)
        bucket["last_success_at"] = time.time()
        return audio, audio_format

    def _engine_stats_summary(self, name: str) -> dict | None:
        bucket = self._engine_stats.get(name)
        if not bucket or (bucket["success"] == 0 and bucket["fail"] == 0):
            return None
        success = bucket["success"]
        return {
            "success": success,
            "fail": bucket["fail"],
            "avg_ms": int(bucket["total_ms"] / success) if success else None,
            "max_ms": bucket["max_ms"],
            "last_error": bucket["last_error"],
            "last_error_at": bucket["last_error_at"],
            "last_success_at": bucket["last_success_at"],
        }

    def _should_skip_tts(self, text: str) -> str:
        """判断文本是否不适合朗读，返回跳过原因（空串表示不跳过）。"""
        if self.config.get("tts_skip_code_blocks", True) and "```" in text:
            return "code_block"

        # 整条消息基本只有链接：朗读一串 URL 没有意义。
        urls = re.findall(r"https?://\S+", text)
        if urls:
            residue = text
            for url in urls:
                residue = residue.replace(url, "")
            if len(residue.strip()) < 10 and min(len(u) for u in urls) > 40:
                return "url_only"

        patterns = self.config.get("tts_skip_patterns") or []
        if not isinstance(patterns, (list, tuple)):
            return ""
        for raw in patterns:
            pattern = str(raw).strip()
            if not pattern:
                continue
            try:
                if re.search(pattern, text):
                    return f"pattern:{pattern}"
            except re.error as e:
                if pattern not in self._warned_skip_patterns:
                    self._warned_skip_patterns.add(pattern)
                    logger.warning(f"[UniversalTTS] 跳过规则不是有效正则，已忽略: {pattern} ({e})")
        return ""

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
            await self._persist_config()
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
        await self._persist_config()
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

        if not await self._is_astrbot_tts_enabled(event):
            yield event.plain_result("AstrBot 默认 TTS 开关未开启，插件不会接管 TTS")
            return

        session_id = _get_session_id(event)
        engine = self._get_engine_for_session(session_id)
        if not engine:
            yield event.plain_result("当前会话没有可用 TTS 引擎，请检查全局默认或会话绑定状态")
            return

        try:
            audio_bytes, fmt = await self._synthesize_with_stats(engine, text)
            audio_path = self._temp_dir / f"tts_test_{uuid.uuid4().hex}.{fmt}"
            await asyncio.to_thread(audio_path.write_bytes, audio_bytes)
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
                lines.append(f"  {i}. {_format_session_id(sid)} → {self._format_binding_name(name)}")

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

    @filter.command("tts_help")
    async def show_help(self, event: AstrMessageEvent):
        """查看指令帮助。用法: /tts_help"""
        if not _is_slash(event):
            return
        yield event.plain_result(self._render_help_text())

    @filter.command("tts_limit_on")
    async def enable_limit(self, event: AstrMessageEvent):
        """开启 TTS 字数上限。用法: /tts_limit_on"""
        if not _is_slash(event):
            return
        if not await self._ensure_admin_permission(event):
            yield event.plain_result("权限不足：该命令仅管理员可用")
            return
        self.config["tts_text_limit_enable"] = True
        await self._persist_config()
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
        await self._persist_config()
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
        await self._persist_config()
        yield event.plain_result(f"已设置 TTS 字数上限为: {limit} 字符")

    # ------------------------------------------------------------------
    # WebUI（插件可视化视图）后端 API
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        """注册 WebUI 视图所需的后端 API。

        低版本 AstrBot 没有 register_web_api，此时静默跳过；views/ 目录同样会被
        旧版本忽略，因此不会出现"装了插件却加载失败"的情况。
        """
        register = getattr(self._context, "register_web_api", None)
        if not callable(register) or json_response is None:
            logger.info(
                "[UniversalTTS] 当前 AstrBot 版本不支持插件 Web API，"
                "WebUI 引擎管理页不可用（原有配置表单不受影响）"
            )
            return
        prefix = f"/{PLUGIN_NAME}"
        apis = (
            (f"{prefix}/engines", self.web_list_engines, ["GET"], "列出 TTS 引擎实例"),
            (f"{prefix}/engines/save", self.web_save_engine, ["POST"], "新增或更新引擎实例"),
            (f"{prefix}/engines/clone", self.web_clone_engine, ["POST"], "复制引擎实例"),
            (f"{prefix}/engines/delete", self.web_delete_engine, ["POST"], "删除引擎实例"),
            (f"{prefix}/engines/activate", self.web_activate_engine, ["POST"], "设置默认引擎"),
            (f"{prefix}/engines/test", self.web_test_engine, ["POST"], "提交试听任务"),
            (f"{prefix}/engines/test/result", self.web_test_result, ["GET"], "查询试听结果"),
            (f"{prefix}/samples", self.web_list_samples, ["GET"], "列出音色样本文件"),
            (f"{prefix}/samples/upload", self.web_upload_sample, ["POST"], "上传音色样本文件"),
            (f"{prefix}/samples/download", self.web_download_sample, ["GET"], "下载音色样本文件"),
            (f"{prefix}/samples/rename", self.web_rename_sample, ["POST"], "重命名音色样本文件"),
            (f"{prefix}/samples/delete", self.web_delete_sample, ["POST"], "删除音色样本文件"),
            (f"{prefix}/settings", self.web_get_settings, ["GET"], "读取全局设置"),
            (f"{prefix}/settings/save", self.web_save_settings, ["POST"], "保存全局设置"),
            (f"{prefix}/engines/reorder", self.web_reorder_engines, ["POST"], "调整引擎顺序"),
            (f"{prefix}/bindings", self.web_list_bindings, ["GET"], "列出会话绑定"),
            (f"{prefix}/bindings/set", self.web_set_binding, ["POST"], "设置会话绑定"),
            (f"{prefix}/bindings/delete", self.web_delete_binding, ["POST"], "解除会话绑定"),
            (f"{prefix}/bindings/clear", self.web_clear_bindings, ["POST"], "清空会话绑定"),
            (f"{prefix}/config/export", self.web_export_config, ["GET"], "导出插件配置"),
            (f"{prefix}/config/import", self.web_import_config, ["POST"], "导入插件配置"),
            (f"{prefix}/history", self.web_list_history, ["GET"], "查看最近变更"),
            (f"{prefix}/help", self.web_get_help, ["GET"], "读取指令帮助与运行状态"),
        )
        for route, handler, methods, desc in apis:
            try:
                register(route, handler, methods, desc)
            except Exception as e:
                logger.error(f"[UniversalTTS] 注册 Web API 失败 {route}: {e}")
        logger.debug(f"[UniversalTTS] 已注册 {len(apis)} 个 Web API 路由")

    def _engine_templates(self) -> dict:
        """读取 _conf_schema.json 中的引擎模板定义，供前端动态渲染表单。"""
        if self._templates_cache is not None:
            return self._templates_cache
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            node = (schema or {}).get("engines") or {}
            templates = node.get("templates") or {}
            self._templates_cache = templates if isinstance(templates, dict) else {}
        except Exception as e:
            logger.warning(f"[UniversalTTS] 读取引擎模板定义失败: {e}")
            self._templates_cache = {}
        return self._templates_cache

    @staticmethod
    def _is_sensitive_field(name: str) -> bool:
        """判断字段是否为密钥类字段（需要在 WebUI 中脱敏）。"""
        return bool(_SENSITIVE_FIELD_RE.search(name or ""))

    def _runtime_switches(self) -> dict:
        """读取真正决定插件是否生效的开关。

        ``global_tts_enabled`` 是 AstrBot 全局 TTS 开关（``provider_tts_settings.enable``）：
        关闭时插件不会接管合成，而用户往往以为"插件坏了"。读取不到时返回 None，
        前端据此不做提示，避免误报。
        """
        global_tts_enabled = None
        try:
            astrbot_config = (
                getattr(self.context, "astrbot_config", None)
                or getattr(self.context, "_config", None)
            )
            if astrbot_config is not None:
                settings = astrbot_config.get("provider_tts_settings", {}) or {}
                global_tts_enabled = bool(settings.get("enable", False))
        except Exception as e:
            logger.debug(f"[UniversalTTS] 读取 AstrBot 全局 TTS 开关失败: {e}")
            global_tts_enabled = None
        return {
            "plugin_enabled": bool(self.config.get("enable", True)),
            "global_tts_enabled": global_tts_enabled,
        }

    def _build_engines_payload(self) -> dict:
        """构造返回给 WebUI 的引擎列表（密钥脱敏，并标记哪些密钥已设置）。"""
        engines: list[dict] = []
        for ec in self._list_engines():
            if not isinstance(ec, dict):
                continue
            item = dict(ec)
            secrets = [
                key
                for key in item
                if isinstance(key, str)
                and self._is_sensitive_field(key)
                and str(item.get(key) or "").strip()
            ]
            for key in secrets:
                item[key] = ""
            item["_secrets"] = secrets
            item["stats"] = self._engine_stats_summary(str(item.get("instance_name") or ""))
            engines.append(item)
        payload = {
            "engines": engines,
            "active_engine": str(self.config.get("active_engine") or ""),
            "disabled_engine": DISABLED_ENGINE,
            "templates": self._engine_templates(),
        }
        payload.update(self._runtime_switches())
        payload["token"] = self._config_token()
        payload["status"] = self._status_summary()
        return payload

    @staticmethod
    def _coerce_field(value, field_type: str, default):
        """按 schema 声明的类型转换前端提交的值。"""
        if field_type == "int":
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default if isinstance(default, int) else 0
        if field_type == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                return default if isinstance(default, float) else 0.0
        if field_type == "bool":
            if isinstance(value, bool):
                return value
            if value is None:
                return bool(default)
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return "" if value is None else str(value)

    def _normalize_engine(
        self, template_key: str, incoming: dict, existing: dict | None
    ) -> dict:
        """按模板定义清洗前端提交的引擎字段，避免脏数据写进配置文件。"""
        template = self._engine_templates().get(template_key)
        if not isinstance(template, dict):
            raise ValueError(f"未知的引擎模板: {template_key}")
        items = template.get("items") or {}
        existing = existing or {}
        result: dict = dict(existing)  # 保留模板未声明、但用户手工添加的字段
        for field, spec in items.items():
            if not isinstance(spec, dict):
                continue
            field_type = str(spec.get("type") or "string")
            default = spec.get("default")
            if self._is_sensitive_field(field):
                submitted = incoming.get(field)
                # 前端拿到的是脱敏空值：留空表示沿用原值，避免误清空密钥
                if submitted is None or str(submitted).strip() == "":
                    result[field] = existing.get(field, default if default is not None else "")
                else:
                    result[field] = self._coerce_field(submitted, field_type, default)
                continue
            if field not in incoming:
                # 前端未提交该字段：优先沿用已有值，否则回落到 schema 默认值
                if field in existing:
                    result[field] = existing[field]
                else:
                    result[field] = self._coerce_field(default, field_type, default)
                continue
            result[field] = self._coerce_field(incoming.get(field), field_type, default)
        result["__template_key"] = template_key
        return result

    @staticmethod
    def _as_index(value, limit: int) -> int | None:
        """把前端传来的 index 规范成合法下标；None/空串表示"新增"。"""
        if value is None or value == "":
            return None
        try:
            idx = int(value)
        except (TypeError, ValueError):
            raise ValueError("index 必须是整数")
        if idx < 0 or idx >= limit:
            raise ValueError("index 超出范围")
        return idx

    async def _apply_engines(self, engines: list[dict]) -> None:
        """写回引擎列表、持久化配置，并重建引擎实例使其立即生效。"""
        self.config["engines"] = engines
        await self._persist_config()
        await self._reload_engine_instances()

    async def _reload_engine_instances(self) -> None:
        """按最新配置重建引擎实例（WebUI 改动后无需重启 AstrBot）。"""
        stale: list[TTSEngine] = []
        seen: set[int] = set()
        candidates = ([self._engine] if self._engine else []) + list(
            self._cached_engines.values()
        )
        for engine in candidates:
            if engine is not None and id(engine) not in seen:
                seen.add(id(engine))
                stale.append(engine)
        self._engine = None
        self._cached_engines.clear()
        for engine in stale:
            try:
                await engine.close()
            except Exception as e:
                logger.warning(f"[UniversalTTS] 关闭旧引擎实例失败: {e}")

        if not self.config.get("enable", True):
            return
        engines_config = [e for e in self._list_engines() if isinstance(e, dict)]
        if not engines_config:
            logger.warning("[UniversalTTS] 引擎列表为空，TTS 不会生效")
            return
        active_name = str(self.config.get("active_engine") or "").strip()
        if active_name == DISABLED_ENGINE:
            return
        target = None
        if active_name:
            for ec in engines_config:
                if str(ec.get("instance_name") or "").strip() == active_name:
                    target = ec
                    break
        if target is None:
            target = engines_config[0]
        try:
            self._engine = get_engine(target, self.config)
            logger.info(
                f"[UniversalTTS] 引擎已重新加载: {self._engine.instance_name} "
                f"(类型: {target.get('__template_key', '')})"
            )
        except Exception as e:
            logger.error(f"[UniversalTTS] 重建引擎实例失败: {type(e).__name__}: {e}")

    async def web_list_engines(self):
        return json_response(self._build_engines_payload())

    async def web_save_engine(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        raw = payload.get("engine")
        if not isinstance(raw, dict):
            return error_response("缺少 engine 字段")
        template_key = str(raw.get("__template_key") or "").strip()
        if not template_key:
            return error_response("缺少 __template_key")
        if template_key not in self._engine_templates():
            return error_response(f"未知的引擎模板: {template_key}")

        engines = [dict(e) for e in self._list_engines() if isinstance(e, dict)]
        try:
            index = self._as_index(payload.get("index"), len(engines))
        except ValueError as e:
            return error_response(str(e))
        existing = engines[index] if index is not None else None

        try:
            normalized = self._normalize_engine(template_key, raw, existing)
        except ValueError as e:
            return error_response(str(e))

        name = str(normalized.get("instance_name") or "").strip()
        if not name:
            return error_response("实例名称不能为空")
        for i, ec in enumerate(engines):
            if index is not None and i == index:
                continue
            if str(ec.get("instance_name") or "").strip() == name:
                return error_response(f"实例名称已存在: {name}")
        normalized["instance_name"] = name

        if index is None:
            engines.append(normalized)
        else:
            engines[index] = normalized

        active = str(self.config.get("active_engine") or "").strip()
        if existing is not None and active:
            old_name = str(existing.get("instance_name") or "").strip()
            if active == old_name and old_name != name:
                self.config["active_engine"] = name
        if not active and engines:
            self.config["active_engine"] = str(engines[0].get("instance_name") or "").strip()

        await self._apply_engines(engines)
        self._append_history(
            "engines.save",
            f"{'更新' if existing is not None else '新建'}引擎「{name}」({template_key})",
        )
        return json_response(self._build_engines_payload())

    async def web_clone_engine(self):
        payload = await request.json(default={})
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        engines = [dict(e) for e in self._list_engines() if isinstance(e, dict)]
        try:
            index = self._as_index(payload.get("index"), len(engines))
        except ValueError as e:
            return error_response(str(e))
        if index is None:
            return error_response("缺少 index")

        source = dict(engines[index])
        source.pop("_secrets", None)
        base = str(source.get("instance_name") or "").strip() or str(
            source.get("__template_key") or "engine"
        )
        names = {str(e.get("instance_name") or "").strip() for e in engines}
        candidate = f"{base} 副本"
        seq = 2
        while candidate in names:
            candidate = f"{base} 副本{seq}"
            seq += 1
        source["instance_name"] = candidate
        engines.insert(index + 1, source)
        await self._apply_engines(engines)
        self._append_history("engines.clone", f"复制引擎「{base}」为「{candidate}」")
        return json_response(self._build_engines_payload())

    async def web_delete_engine(self):
        payload = await request.json(default={})
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        engines = [dict(e) for e in self._list_engines() if isinstance(e, dict)]
        try:
            index = self._as_index(payload.get("index"), len(engines))
        except ValueError as e:
            return error_response(str(e))
        if index is None:
            return error_response("缺少 index")

        removed = engines.pop(index)
        removed_name = str(removed.get("instance_name") or "").strip()
        if str(self.config.get("active_engine") or "").strip() == removed_name:
            self.config["active_engine"] = (
                str(engines[0].get("instance_name") or "").strip() if engines else ""
            )
        stale = [sid for sid, bound in self._session_bindings.items() if bound == removed_name]
        for sid in stale:
            del self._session_bindings[sid]
        if stale:
            self._save_bindings()
        await self._apply_engines(engines)
        self._append_history("engines.delete", f"删除引擎「{removed_name}」")
        return json_response(self._build_engines_payload())

    async def web_activate_engine(self):
        payload = await request.json(default={})
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        value = str(payload.get("name") or "").strip()
        if value == DISABLED_ENGINE:
            self.config["active_engine"] = DISABLED_ENGINE
            await self._persist_config()
            await self._reload_engine_instances()
            self._append_history("engines.activate", "关闭全局 TTS")
            return json_response(self._build_engines_payload())
        if not value:
            return error_response("缺少 name")
        names = {
            str(e.get("instance_name") or "").strip()
            for e in self._list_engines()
            if isinstance(e, dict)
        }
        if value not in names:
            return error_response(f"未找到引擎: {value}")
        self.config["active_engine"] = value
        await self._persist_config()
        await self._reload_engine_instances()
        self._append_history("engines.activate", f"设为默认引擎「{value}」")
        return json_response(self._build_engines_payload())

    # ------------------------------------------------------------------
    # 试听
    # ------------------------------------------------------------------

    async def web_test_engine(self):
        """提交一次试听任务，立即返回 job_id。

        ``{index: n}`` 试听已保存的引擎；``{index?: n, engine: {...}}`` 试听编辑器
        里的草稿。合成放到后台任务里做，由前端轮询 ``engines/test/result`` 取结果。

        之所以不在这里同步等待：视图经由反向代理访问，单个请求挂太久会被
        中间层直接掐断并返回 524（实测不到 30 秒就会发生），拆成短请求更稳。
        """
        payload = await request.json(default={}) or {}
        raw_engines = self._list_engines()
        try:
            index = (
                self._as_index(payload.get("index"), len(raw_engines))
                if payload.get("index") is not None
                else None
            )
        except ValueError as e:
            return error_response(str(e))

        existing = dict(raw_engines[index]) if index is not None else None
        draft = payload.get("engine")
        if isinstance(draft, dict):
            template_key = str(
                draft.get("__template_key")
                or (existing or {}).get("__template_key")
                or ""
            ).strip()
            if not template_key:
                return error_response("缺少引擎类型 __template_key")
            try:
                engine_config = self._normalize_engine(template_key, draft, existing)
            except ValueError as e:
                return error_response(str(e))
        elif existing is not None:
            engine_config = existing
        else:
            return error_response("需要提供 index 或 engine 参数")

        self._prune_preview_jobs()
        job_id = uuid.uuid4().hex
        self._preview_jobs[job_id] = {"status": "pending", "created": time.time()}
        asyncio.create_task(self._run_preview_job(job_id, engine_config))
        logger.debug(f"[UniversalTTS] 试听任务已提交: {job_id}")
        return json_response({"job_id": job_id})

    async def web_test_result(self):
        """轮询试听任务的结果。"""
        job_id = str(request.query.get("job_id", "") or "").strip()
        if not job_id:
            return error_response("缺少 job_id 参数")
        job = self._preview_jobs.get(job_id)
        if job is None:
            return error_response("试听任务不存在或已过期，请重试")
        result = {
            "status": job.get("status", "pending"),
            "elapsed_ms": int((time.time() - job.get("created", time.time())) * 1000),
        }
        for key in ("audio", "format", "bytes", "playable", "text", "error", "took_ms"):
            if key in job:
                result[key] = job[key]
        return json_response(result)

    def _prune_preview_jobs(self) -> None:
        """清掉过期任务，并保证任务表不会无限增长。"""
        now = time.time()
        for key in [
            k
            for k, v in self._preview_jobs.items()
            if now - v.get("created", now) > PREVIEW_JOB_TTL
        ]:
            self._preview_jobs.pop(key, None)
        while len(self._preview_jobs) >= PREVIEW_MAX_JOBS:
            oldest = min(
                self._preview_jobs, key=lambda k: self._preview_jobs[k].get("created", 0)
            )
            self._preview_jobs.pop(oldest, None)

    async def _run_preview_job(self, job_id: str, engine_config: dict) -> None:
        job = self._preview_jobs.get(job_id)
        if job is None:
            return

        engine = None
        started = time.monotonic()
        try:
            engine = get_engine(engine_config, self.config)
            audio, audio_format = await asyncio.wait_for(
                engine.synthesize(PREVIEW_TEXT), timeout=PREVIEW_TIMEOUT
            )
        except asyncio.TimeoutError:
            job.update(
                status="error",
                error=f"合成超时（超过 {PREVIEW_TIMEOUT} 秒），请检查接口状态或网络",
            )
            return
        except Exception as e:
            logger.warning(f"[UniversalTTS] 试听合成失败: {e}")
            job.update(status="error", error=self._friendly_engine_error(e))
            return
        finally:
            if engine is not None:
                try:
                    await engine.close()
                except Exception:
                    pass

        if not audio:
            job.update(status="error", error="引擎返回了空音频")
            return
        if len(audio) > PREVIEW_MAX_BYTES:
            job.update(
                status="error",
                error=f"试听音频过大（{len(audio)} 字节），超过 {PREVIEW_MAX_BYTES} 字节上限",
            )
            return

        fmt = str(audio_format or "").strip().lower().lstrip(".")
        job.update(
            status="done",
            audio=(
                f"data:{_AUDIO_MIME.get(fmt, 'application/octet-stream')};base64,"
                f"{base64.b64encode(audio).decode('ascii')}"
            ),
            format=fmt,
            bytes=len(audio),
            playable=fmt in _PLAYABLE_FORMATS,
            text=PREVIEW_TEXT,
            took_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _friendly_engine_error(exc: Exception) -> str:
        """把常见的上游接口错误翻译成人话。"""
        message = str(exc).strip() or type(exc).__name__
        low = message.lower()
        if "429" in message or "too many requests" in low or "rate limit" in low:
            return "接口返回 429 限流。试听与实际发送共用同一配额，请稍后再试"
        if "401" in message or "403" in message or "unauthorized" in low:
            return "鉴权失败，请检查 API Key 是否正确"
        if "timeout" in low or "timed out" in low:
            return "请求上游接口超时，请稍后重试"
        return message

    # ------------------------------------------------------------------
    # 音色样本
    # ------------------------------------------------------------------

    def _sample_entries(self) -> list[dict]:
        """列出 voice_clone_samples 中的样本文件、存在状态与引用它的引擎。"""
        references: dict[str, list[str]] = {}
        for item in self._list_engines():
            if not isinstance(item, dict):
                continue
            sample = str(item.get("voice_sample_name") or "").strip()
            if not sample:
                continue
            name = str(item.get("instance_name") or "").strip() or "(未命名)"
            references.setdefault(sample, []).append(name)

        entries: list[dict] = []
        for raw in self.config.get("voice_clone_samples", []) or []:
            if not isinstance(raw, str) or not raw.strip():
                continue
            rel = raw.strip()
            path = self._data_dir / rel
            try:
                exists = path.is_file()
                size = path.stat().st_size if exists else 0
            except OSError:
                exists, size = False, 0
            file_name = Path(rel).name
            entries.append(
                {
                    "path": rel,
                    "name": file_name,
                    "exists": exists,
                    "size": size,
                    "referenced_by": references.get(file_name, []),
                }
            )
        return entries

    async def web_list_samples(self):
        return json_response({"samples": self._sample_entries()})

    async def web_upload_sample(self):
        try:
            files = await request.files()
        except Exception as e:
            return error_response(f"读取上传内容失败: {e}")
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("未收到文件，表单字段名需为 file")

        safe = Path(str(upload.filename or "").replace("\\", "/")).name.strip()
        if not safe or safe in (".", ".."):
            return error_response("文件名为空或不合法")
        suffix = Path(safe).suffix.lower()
        if suffix not in SAMPLE_EXTS:
            allowed = " / ".join(sorted(SAMPLE_EXTS))
            return error_response(f"不支持的格式 {suffix or '(无扩展名)'}，仅支持 {allowed}")

        target_dir = self._data_dir / SAMPLE_REL_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(safe).stem[:80] or "sample"
        candidate = f"{stem}{suffix}"
        seq = 2
        while (target_dir / candidate).exists():
            candidate = f"{stem}_{seq}{suffix}"
            seq += 1
        dest = target_dir / candidate

        try:
            written = await upload.save(dest, max_bytes=SAMPLE_MAX_BYTES)
        except Exception as e:
            try:
                dest.unlink()
            except OSError:
                pass
            return error_response(f"保存样本失败: {e}")

        rel = f"{SAMPLE_REL_DIR}/{candidate}"
        samples = [
            s
            for s in (self.config.get("voice_clone_samples", []) or [])
            if isinstance(s, str)
        ]
        if rel not in samples:
            samples.append(rel)
        self.config["voice_clone_samples"] = samples
        await self._persist_config()
        await self._reload_engine_instances()
        logger.info(f"[UniversalTTS] 已上传音色样本 {candidate}（{written} 字节）")
        self._append_history("samples.upload", f"上传样本「{candidate}」（{written} 字节）")
        return json_response(
            {
                "samples": self._sample_entries(),
                "path": rel,
                "name": candidate,
                "bytes": written,
            }
        )

    async def web_delete_sample(self):
        payload = await request.json(default={}) or {}
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        rel = str(payload.get("path") or "").strip()
        if not rel:
            return error_response("缺少 path 参数")
        samples = [
            s
            for s in (self.config.get("voice_clone_samples", []) or [])
            if isinstance(s, str)
        ]
        if rel not in samples:
            return error_response(f"样本不存在: {rel}")

        name = Path(rel).name
        referenced = [
            str(item.get("instance_name") or "").strip() or "(未命名)"
            for item in self._list_engines()
            if isinstance(item, dict)
            and str(item.get("voice_sample_name") or "").strip() == name
        ]
        if referenced:
            return error_response(
                "该样本正被以下引擎引用，请先换掉它们的音色样本：" + "、".join(referenced)
            )

        samples.remove(rel)
        self.config["voice_clone_samples"] = samples
        await self._persist_config()
        try:
            (self._data_dir / rel).unlink()
        except OSError as e:
            logger.warning(f"[UniversalTTS] 删除样本文件失败 {rel}: {e}")
        await self._reload_engine_instances()
        logger.info(f"[UniversalTTS] 已删除音色样本 {name}")
        self._append_history("samples.delete", f"删除样本「{name}」")
        return json_response({"samples": self._sample_entries()})

    async def web_download_sample(self):
        """下载音色样本文件（视图侧通过 bridge.download 触发）。"""
        if file_response is None:
            return error_response("当前 AstrBot 版本不支持文件下载")
        rel = str(request.query.get("path", "") or "").strip()
        if not rel:
            return error_response("缺少 path 参数")
        samples = [
            s
            for s in (self.config.get("voice_clone_samples", []) or [])
            if isinstance(s, str)
        ]
        if rel not in samples:
            return error_response(f"样本不存在: {rel}")
        path = self._data_dir / rel
        if not path.is_file():
            return error_response("样本文件不存在，可能已被移动或删除")
        name = Path(rel).name
        return file_response(
            path,
            filename=name,
            content_type=_AUDIO_MIME.get(Path(name).suffix.lower().lstrip("."), "application/octet-stream"),
        )

    async def web_rename_sample(self):
        """重命名音色样本文件，并同步更新引用它的引擎。"""
        payload = await request.json(default={}) or {}
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        rel = str(payload.get("path") or "").strip()
        new_name = Path(str(payload.get("new_name") or "").replace("\\", "/")).name.strip()
        if not rel:
            return error_response("缺少 path 参数")
        if not new_name:
            return error_response("新文件名不能为空")
        if new_name in (".", ".."):
            return error_response("非法的文件名")
        suffix = Path(new_name).suffix.lower()
        if suffix not in SAMPLE_EXTS:
            allowed = " / ".join(sorted(SAMPLE_EXTS))
            return error_response(f"不支持的格式 {suffix or '(无扩展名)'}，仅支持 {allowed}")

        samples = [
            s
            for s in (self.config.get("voice_clone_samples", []) or [])
            if isinstance(s, str)
        ]
        if rel not in samples:
            return error_response(f"样本不存在: {rel}")

        old_name = Path(rel).name
        source = self._data_dir / rel
        if not source.is_file():
            return error_response("样本文件不存在，无法重命名")

        dest = self._data_dir / SAMPLE_REL_DIR / new_name
        try:
            if dest.resolve() == source.resolve():
                return json_response({"samples": self._sample_entries(), "renamed_refs": 0})
        except OSError:
            pass
        if dest.exists():
            return error_response(f"已存在同名样本: {new_name}")

        try:
            source.rename(dest)
        except OSError as e:
            return error_response(f"重命名文件失败: {e}")

        new_rel = f"{SAMPLE_REL_DIR}/{new_name}"
        samples[samples.index(rel)] = new_rel
        self.config["voice_clone_samples"] = samples

        renamed = 0
        for item in self._list_engines():
            if not isinstance(item, dict):
                continue
            if str(item.get("voice_sample_name") or "").strip() == old_name:
                item["voice_sample_name"] = new_name
                renamed += 1

        await self._persist_config()
        await self._reload_engine_instances()
        logger.info(
            f"[UniversalTTS] 样本已重命名: {old_name} -> {new_name}（同步 {renamed} 个引擎）"
        )
        self._append_history(
            "samples.rename", f"样本「{old_name}」→「{new_name}」（同步 {renamed} 个引擎）"
        )
        return json_response(
            {
                "samples": self._sample_entries(),
                "path": new_rel,
                "name": new_name,
                "renamed_refs": renamed,
            }
        )

    # ------------------------------------------------------------------
    # 全局设置
    # ------------------------------------------------------------------

    # 这些字段各有专门界面，不在「全局设置」里重复出现
    _SETTINGS_EXCLUDED = {"engines", "active_engine", "voice_clone_samples"}

    def _read_schema(self) -> dict:
        """读取并缓存 _conf_schema.json。"""
        if self._schema_cache is not None:
            return self._schema_cache
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            data = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            self._schema_cache = data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[UniversalTTS] 读取配置结构失败: {e}")
            self._schema_cache = {}
        return self._schema_cache

    def _settings_schema(self) -> list[tuple[str, dict]]:
        return [
            (key, spec)
            for key, spec in self._read_schema().items()
            if isinstance(spec, dict) and key not in self._SETTINGS_EXCLUDED
        ]

    def _build_settings_payload(self) -> dict:
        fields: list[dict] = []
        secrets: list[str] = []
        for key, spec in self._settings_schema():
            default = spec.get("default")
            value = self.config.get(key, default)
            if self._is_sensitive_field(key):
                if str(value or "").strip():
                    secrets.append(key)
                value = ""
            fields.append(
                {
                    "key": key,
                    "type": str(spec.get("type") or "string"),
                    "description": str(spec.get("description") or key),
                    "hint": str(spec.get("hint") or ""),
                    "default": default,
                    "options": spec.get("options") or [],
                    "value": value,
                }
            )
        return {"fields": fields, "secrets": secrets, "token": self._config_token()}

    def _coerce_setting(self, value, field_type: str, default):
        """按 schema 类型归一化设置值；list 类型接受数组或分隔文本。"""
        if field_type == "list":
            items = value if isinstance(value, list) else re.split(r"[\n,，;；]+", str(value or ""))
            return [str(item).strip() for item in items if str(item).strip()]
        if field_type in ("int", "float"):
            # _coerce_field 遇到非法数字会静默退回默认值，这里必须显式报错，
            # 否则用户填错数字会被无声吞掉。
            text = str(value).strip()
            if not text:
                return default
            try:
                return int(float(text)) if field_type == "int" else float(text)
            except (TypeError, ValueError):
                raise ValueError("需要填写整数" if field_type == "int" else "需要填写数字")
        return self._coerce_field(value, field_type, default)

    async def web_get_settings(self):
        """读取全局设置（敏感字段脱敏）。"""
        return json_response(self._build_settings_payload())

    async def web_save_settings(self):
        """保存全局设置；敏感字段留空表示保持原值。"""
        payload = await request.json(default={}) or {}
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        incoming = payload.get("values")
        if not isinstance(incoming, dict):
            return error_response("缺少 values 参数")

        updated: dict = {}
        for key, spec in self._settings_schema():
            if key not in incoming:
                continue
            label = str(spec.get("description") or key)
            if self._is_sensitive_field(key) and not str(incoming.get(key) or "").strip():
                continue
            try:
                updated[key] = self._coerce_setting(
                    incoming[key], str(spec.get("type") or "string"), spec.get("default")
                )
            except ValueError as e:
                return error_response(f"{label}: {e}")

        if not updated:
            return error_response("没有任何有效改动")

        for key, value in updated.items():
            self.config[key] = value
        await self._persist_config()
        await self._reload_engine_instances()
        logger.info(f"[UniversalTTS] 已更新全局设置: {', '.join(sorted(updated))}")
        self._append_history(
            "settings.save", f"更新设置: {', '.join(sorted(updated))}"
        )
        return json_response(self._build_settings_payload())

    # ------------------------------------------------------------------
    # 配置指纹（双入口冲突检测）
    # ------------------------------------------------------------------

    def _config_token(self) -> str:
        """当前配置的指纹。

        WebUI 视图和插件配置表单都能改同一份配置。前端把读取时的指纹带回来，
        保存前比对，就能发现"另一边已经改过了"，避免后保存的静默覆盖前者。
        """
        try:
            payload = json.dumps(
                self.config, ensure_ascii=False, sort_keys=True, default=str
            )
        except Exception:
            return ""
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _check_config_token(self, payload: dict) -> str | None:
        """校验前端携带的指纹；返回错误信息，或在无需拦截时返回 None。"""
        expected = str((payload or {}).get("token") or "").strip()
        if not expected:
            # 没有携带指纹（老客户端或非视图调用）不做拦截
            return None
        if expected != self._config_token():
            return "配置已被其他方式修改（例如插件配置表单），请刷新页面后重试"
        return None

    # ------------------------------------------------------------------
    # 变更历史
    # ------------------------------------------------------------------

    HISTORY_MAX_ENTRIES = 200
    HISTORY_FILE_NAME = "webui_history.jsonl"

    def _append_history(self, action: str, detail: str) -> None:
        """记录一次改动，供「最近变更」查看。调用方需自行避免写入敏感内容。"""
        entry = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "detail": detail,
        }
        try:
            path = self._data_dir / self.HISTORY_FILE_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._trim_history(path)
        except Exception as e:
            logger.warning(f"[UniversalTTS] 写入变更历史失败: {e}")

    def _trim_history(self, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.HISTORY_MAX_ENTRIES:
            return
        try:
            path.write_text(
                "\n".join(lines[-self.HISTORY_MAX_ENTRIES:]) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def _read_history(self, limit: int = 30) -> list[dict]:
        path = self._data_dir / self.HISTORY_FILE_NAME
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries

    async def web_list_history(self):
        raw = str(request.query.get("limit", "30") or "30")
        try:
            limit = max(1, min(self.HISTORY_MAX_ENTRIES, int(raw)))
        except (TypeError, ValueError):
            limit = 30
        return json_response({"entries": self._read_history(limit)})

    # ------------------------------------------------------------------
    # 状态总览
    # ------------------------------------------------------------------

    def _status_summary(self) -> dict:
        engines = [e for e in self._list_engines() if isinstance(e, dict)]
        names = {
            str(e.get("instance_name") or "").strip()
            for e in engines
            if str(e.get("instance_name") or "").strip()
        }
        active = str(self.config.get("active_engine") or "").strip()
        samples = [
            s for s in (self.config.get("voice_clone_samples") or []) if isinstance(s, str)
        ]
        return {
            "engine_count": len(engines),
            "binding_count": len(self._session_bindings),
            "sample_count": len(samples),
            "active_engine": active,
            "active_engine_exists": (
                None
                if not active or active == DISABLED_ENGINE
                else active in names
            ),
        }

    # ------------------------------------------------------------------
    # 指令帮助
    # ------------------------------------------------------------------

    def _help_snapshot(self) -> dict:
        """帮助页顶部的实时状态，避免文档与真实配置脱节。"""
        switches = self._runtime_switches()
        return {
            "plugin_enabled": switches.get("plugin_enabled"),
            "global_tts_enabled": switches.get("global_tts_enabled"),
            "runtime_enabled": self._runtime_enabled,
            "active_engine": self._global_engine_name(),
            "engine_count": len(self._list_engines()),
            "binding_count": len(self._session_bindings),
            "text_limit": int(self.config.get("tts_text_limit", 50) or 0),
            "text_limit_enabled": bool(self.config.get("tts_text_limit_enable", False)),
            "admin_only": bool(self.config.get("admin_only_commands", True)),
            "admin_count": len(
                [
                    item
                    for item in (self.config.get("admin_user_ids") or [])
                    if str(item).strip()
                ]
            ),
        }

    def _render_help_text(self) -> str:
        """/tts_help 的纯文本输出，与 WebUI 帮助页共用 COMMAND_SPECS。"""
        snap = self._help_snapshot()
        lines = [
            "通用 TTS 指令（均需以 / 开头）",
            "",
            f"当前状态：{'已启用' if snap['plugin_enabled'] else '已停用'}"
            f" · 全局引擎 {snap['active_engine']}"
            f" · 会话绑定 {snap['binding_count']} 个"
            f" · 字数上限 {'开启' if snap['text_limit_enabled'] else '关闭'}",
            "",
            "—— 普通指令 ——",
        ]
        for spec in COMMAND_SPECS:
            if spec["admin"]:
                continue
            lines.append(spec["usage"])
            lines.append(f"  {spec['summary']}")
            for note in spec["notes"]:
                lines.append(f"  · {note}")
        lines.append("")
        lines.append("—— 管理员指令 ——")
        for spec in COMMAND_SPECS:
            if not spec["admin"]:
                continue
            lines.append(spec["usage"])
            lines.append(f"  {spec['summary']}")
            for note in spec["notes"]:
                lines.append(f"  · {note}")
        lines.append("")
        lines.append("提示：管理员指令在 WebUI 里都有对应的可视化操作。")
        return "\n".join(lines)

    async def web_get_help(self):
        return json_response(
            {"commands": COMMAND_SPECS, "snapshot": self._help_snapshot()}
        )

    # ------------------------------------------------------------------
    # 会话绑定
    # ------------------------------------------------------------------

    def _binding_entries(self) -> list[dict]:
        names = {
            str(e.get("instance_name") or "").strip()
            for e in self._list_engines()
            if isinstance(e, dict)
        }
        entries: list[dict] = []
        for session_id in sorted(self._session_bindings):
            target = str(self._session_bindings.get(session_id) or "").strip()
            try:
                display = _format_session_id(session_id)
            except Exception:
                display = session_id
            entries.append(
                {
                    "session_id": session_id,
                    "display": display,
                    "target": target,
                    "target_label": self._format_binding_name(target),
                    # 指向已被删除的引擎即为悬空绑定
                    "exists": True if target == DISABLED_ENGINE else target in names,
                }
            )
        return entries

    def _bindings_payload(self) -> dict:
        return {
            "bindings": self._binding_entries(),
            "activity": self._activity_entries(),
            "engines": sorted(
                {
                    str(e.get("instance_name") or "").strip()
                    for e in self._list_engines()
                    if isinstance(e, dict) and str(e.get("instance_name") or "").strip()
                }
            ),
            "disabled_engine": DISABLED_ENGINE,
            "token": self._config_token(),
            "active_engine": str(self.config.get("active_engine") or ""),
        }

    async def web_list_bindings(self):
        return json_response(self._bindings_payload())

    async def web_set_binding(self):
        payload = await request.json(default={}) or {}
        session_id = str(payload.get("session_id") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not session_id:
            return error_response("缺少 session_id 参数")
        if not target:
            self._session_bindings.pop(session_id, None)
            summary = "恢复为跟随全局引擎"
        else:
            if target != DISABLED_ENGINE:
                names = {
                    str(e.get("instance_name") or "").strip()
                    for e in self._list_engines()
                    if isinstance(e, dict)
                }
                if target not in names:
                    return error_response(f"引擎不存在: {target}")
            self._session_bindings[session_id] = target
            summary = f"绑定到 {self._format_binding_name(target)}"
        self._save_bindings()
        self._append_history(
            "bindings.set", f"{_format_session_id(session_id)}：{summary}"
        )
        return json_response(self._bindings_payload())

    async def web_delete_binding(self):
        payload = await request.json(default={}) or {}
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return error_response("缺少 session_id 参数")
        if session_id not in self._session_bindings:
            return error_response(f"绑定不存在: {session_id}")
        self._session_bindings.pop(session_id, None)
        self._save_bindings()
        self._append_history(
            "bindings.delete", f"解除 {_format_session_id(session_id)} 的绑定"
        )
        return json_response(self._bindings_payload())

    async def web_clear_bindings(self):
        count = len(self._session_bindings)
        if not count:
            return error_response("当前没有会话绑定")
        self._session_bindings.clear()
        self._save_bindings()
        self._append_history("bindings.clear", f"清空 {count} 条会话绑定")
        return json_response(self._bindings_payload())

    # ------------------------------------------------------------------
    # 引擎排序
    # ------------------------------------------------------------------

    async def web_reorder_engines(self):
        payload = await request.json(default={}) or {}
        stale = self._check_config_token(payload)
        if stale:
            return error_response(stale)
        order = payload.get("order")
        engines = self._list_engines()
        if not isinstance(order, list) or len(order) != len(engines):
            return error_response("排序参数与引擎数量不一致，请刷新后重试")
        try:
            indices = [self._as_index(value, len(engines)) for value in order]
        except ValueError as e:
            return error_response(str(e))
        if len(set(indices)) != len(indices):
            return error_response("排序参数存在重复项")
        reordered = [engines[i] for i in indices]
        await self._apply_engines(reordered)
        self._append_history("engines.reorder", f"调整 {len(reordered)} 个引擎的顺序")
        return json_response(self._build_engines_payload())

    # ------------------------------------------------------------------
    # 配置导入 / 导出
    # ------------------------------------------------------------------

    async def web_export_config(self):
        """导出插件配置与会话绑定。默认脱敏，显式要求时才带密钥。"""
        raw = str(request.query.get("include_secrets", "") or "").strip().lower()
        include_secrets = raw in ("1", "true", "yes", "on")
        data = json.loads(json.dumps(self.config, ensure_ascii=False, default=str))
        if not include_secrets:
            for item in data.get("engines") or []:
                if not isinstance(item, dict):
                    continue
                for key in list(item):
                    if self._is_sensitive_field(key):
                        item[key] = ""
        # 会话绑定不是配置 schema 字段，单独快照进导出文件，迁移时不丢绑定。
        data["session_bindings"] = [
            {"session_id": sid, "engine": name}
            for sid, name in self._session_bindings.items()
        ]
        detail = (
            f"导出配置（含密钥）+ {len(self._session_bindings)} 条绑定"
            if include_secrets
            else f"导出配置（已脱敏）+ {len(self._session_bindings)} 条绑定"
        )
        self._append_history("config.export", detail)
        return json_response(
            {
                "config": data,
                "plugin_version": _read_plugin_version("unknown"),
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "include_secrets": include_secrets,
            }
        )

    async def web_import_config(self):
        """导入配置：逐字段按 schema 校验类型，任一字段不合法则整体拒绝。"""
        payload = await request.json(default={}) or {}
        incoming = payload.get("config")
        if not isinstance(incoming, dict):
            return error_response("缺少 config 参数")

        # 会话绑定不属于配置 schema，先取出单独处理。
        incoming_bindings = incoming.pop("session_bindings", None)

        applied: dict = {}
        for key, spec in self._read_schema().items():
            if not isinstance(spec, dict) or key not in incoming:
                continue
            field_type = str(spec.get("type") or "string")
            label = str(spec.get("description") or key)
            value = incoming.get(key)
            try:
                if field_type == "template_list":
                    if not isinstance(value, list):
                        raise ValueError("应为引擎列表")
                    engines: list[dict] = []
                    for idx, item in enumerate(value):
                        if not isinstance(item, dict):
                            raise ValueError(f"第 {idx + 1} 项不是对象")
                        template_key = str(item.get("__template_key") or "").strip()
                        if not template_key:
                            raise ValueError(f"第 {idx + 1} 项缺少 __template_key")
                        engines.append(self._normalize_engine(template_key, item, None))
                    applied[key] = engines
                elif field_type == "file":
                    if not isinstance(value, list):
                        raise ValueError("应为文件列表")
                    applied[key] = [str(v) for v in value if str(v).strip()]
                else:
                    applied[key] = self._coerce_setting(
                        value, field_type, spec.get("default")
                    )
            except ValueError as e:
                return error_response(f"{label}: {e}")

        if not applied:
            return error_response("没有可导入的字段")

        # 绑定导入：整体替换，引擎名必须在（导入后的）引擎列表或禁音值中。
        # 注意：导入只替换显式绑定；活跃记录是只读视图，不受导入影响。
        imported_bindings = 0
        if incoming_bindings is not None:
            if not isinstance(incoming_bindings, list):
                return error_response("session_bindings 应为列表")
            valid_names = {
                str(item.get("instance_name") or "").strip()
                for item in (applied.get("engines") or self._list_engines())
                if isinstance(item, dict)
            } - {""}
            new_bindings: dict[str, str] = {}
            for idx, entry in enumerate(incoming_bindings, 1):
                if not isinstance(entry, dict):
                    return error_response(f"会话绑定第 {idx} 项不是对象")
                sid = str(entry.get("session_id") or "").strip()
                name = str(entry.get("engine") or "").strip()
                if not sid or not name:
                    return error_response(f"会话绑定第 {idx} 项缺少 session_id 或 engine")
                if name != DISABLED_ENGINE and name not in valid_names:
                    return error_response(f"会话绑定第 {idx} 项引用了不存在的引擎: {name}")
                new_bindings[sid] = name
            self._session_bindings = new_bindings
            self._save_bindings()
            imported_bindings = len(new_bindings)

        for key, value in applied.items():
            self.config[key] = value
        await self._persist_config()
        await self._reload_engine_instances()
        detail = f"导入 {len(applied)} 项配置"
        if incoming_bindings is not None:
            detail += f"，替换 {imported_bindings} 条会话绑定"
        self._append_history("config.import", detail)
        logger.info(f"[UniversalTTS] 已导入配置: {', '.join(sorted(applied))}")
        result = self._build_settings_payload()
        result["imported"] = sorted(applied)
        result["imported_bindings"] = imported_bindings
        return json_response(result)

    async def terminate(self):
        """插件卸载时释放引擎资源"""
        if self._engine:
            await self._engine.close()
        for engine in self._cached_engines.values():
            await engine.close()
        self._cached_engines.clear()
        self._save_activity(force=True)
        logger.info("[UniversalTTS] 引擎资源已释放")
