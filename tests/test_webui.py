"""在最小 stub 环境下加载真实插件代码，验证 WebUI 后端逻辑（P0 + P1）。"""

import asyncio
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import yaml
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG = "astrbot_plugin_universal_tts"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}  {detail}")


async def _submit_and_wait(inst, payload, web, retries=300):
    """提交试听任务并轮询到结束。

    合成在后台任务里，必须共用同一个事件循环才能推进，所以整体放进一个
    asyncio.run 里跑。
    """
    web.request.payload = payload
    started = await inst.web_test_engine()
    if not started.get("__ok__"):
        return started
    job_id = (started.get("data") or {}).get("job_id")
    if not job_id:
        return {"__ok__": False, "error": "没有返回 job_id"}
    for _ in range(retries):
        web.request.query = {"job_id": job_id}
        res = await inst.web_test_result()
        if not res.get("__ok__"):
            return res
        if (res.get("data") or {}).get("status") != "pending":
            return res
        await asyncio.sleep(0.01)
    return {"__ok__": False, "error": "轮询超时"}


# --------------------------------------------------------------------------
# 可控的合成行为
# --------------------------------------------------------------------------

STUB = {"audio": b"RIFF" + b"\x00" * 64, "format": "wav", "fail": None}


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Config(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saved = 0

    async def save_config_async(self):
        self.saved += 1


class _DummyFilter:
    def on_decorating_result(self, *_a, **_k):
        return lambda fn: fn

    def on_waiting_llm_request(self, *_a, **_k):
        return lambda fn: fn

    def command(self, *_a, **_k):
        return lambda fn: fn


class _MsgEvent:
    pass


class _Star:
    def __init__(self, context):
        self.context = context


def _register(*_a, **_k):
    return lambda cls: cls


class _Context:
    def __init__(self):
        self.routes = {}

    def register_web_api(self, route, handler, methods, desc):
        self.routes[route] = (handler, tuple(methods), desc)


class _FakeUpload:
    def __init__(self, filename, data=b"ID3-fake-audio-bytes"):
        self.filename = filename
        self.content_type = "audio/wav"
        self.content_length = None
        self._data = data
        self.saved_path = None

    async def save(self, destination, *, max_bytes=None):
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._data)
        self.saved_path = dest
        return len(self._data)

    async def read(self, size=-1):
        return self._data


class _Request:
    def __init__(self):
        self.payload = {}
        self.upload = None
        self.query = {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default

    async def files(self):
        return {"file": self.upload} if self.upload is not None else {}


def _json_response(data):
    return {"__ok__": True, "data": data}


def _error_response(message, status_code=400):
    return {"__ok__": False, "error": message}


def _file_response(path, filename=None, content_type=None, headers=None):
    return {
        "__ok__": True,
        "data": {
            "__file__": str(path),
            "filename": filename,
            "content_type": content_type,
        },
    }


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = _Config
    astrbot.api = api

    comp = types.ModuleType("astrbot.api.message_components")
    event = types.ModuleType("astrbot.api.event")
    event.filter = _DummyFilter()
    event.AstrMessageEvent = _MsgEvent
    star = types.ModuleType("astrbot.api.star")
    star.Context = _Context
    star.Star = _Star
    star.register = _register
    web = types.ModuleType("astrbot.api.web")
    web.request = _Request()
    web.json_response = _json_response
    web.error_response = _error_response
    web.file_response = _file_response

    api.message_components = comp
    api.event = event
    api.star = star
    api.web = web

    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    slm = types.ModuleType("astrbot.core.star.session_llm_manager")
    slm.SessionServiceManager = None
    core.star = core_star
    core_star.session_llm_manager = slm

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.message_components": comp,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
        "astrbot.core": core,
        "astrbot.core.star": core_star,
        "astrbot.core.star.session_llm_manager": slm,
    }.items():
        sys.modules[name] = mod
    return web


def _load_plugin_module():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = pkg

    engines = types.ModuleType(f"{PKG}.engines")

    class TTSEngine:
        def __init__(self, cfg, plugin_config=None):
            self.config = cfg
            self.plugin_config = plugin_config or {}
            self.instance_name = str(cfg.get("instance_name") or "")
            self.closed = False

        async def synthesize(self, text):
            if STUB["fail"]:
                raise RuntimeError(STUB["fail"])
            return STUB["audio"], STUB["format"]

        async def close(self):
            self.closed = True

    engines.TTSEngine = TTSEngine
    engines.get_engine = lambda cfg, conf=None: TTSEngine(cfg, conf)
    sys.modules[f"{PKG}.engines"] = engines
    pkg.engines = engines

    spec = importlib.util.spec_from_file_location(f"{PKG}.main", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.main"] = module
    spec.loader.exec_module(module)
    return module


SAMPLE_ENGINES = [
    {
        "instance_name": "紫苏九月",
        "__template_key": "mimo_v2_5_voiceclone",
        "api_key": "sk-secret-1",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-tts-voiceclone",
        "voice_sample_name": "a.wav",
        "format": "wav",
        "style_prompt": "",
        "timeout": 60,
    },
    {
        "instance_name": "备用",
        "__template_key": "mimo_v2",
        "api_key": "",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2-tts",
        "voice": "mimo_default",
        "format": "wav",
        "style": "",
        "seed_text": "",
        "timeout": "30",
    },
]


def _sensitive_all_empty(engines) -> bool:
    """导出结果里所有密钥类字段是否都为空（且确实存在这类字段）。"""
    names = ("api_key", "access_token", "subscription_key", "token", "secret", "password")
    found = False
    for item in engines or []:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if any(name in str(key) for name in names):
                found = True
                if str(value or "").strip():
                    return False
    return found


def main():
    web = _install_stubs()
    workdir = Path(tempfile.mkdtemp(prefix="tts_test_"))
    # 先记原目录再切换；进程退出时恢复并清理（Windows 下必须先切走才能删除）。
    _orig_cwd = os.getcwd()
    os.chdir(workdir)

    def _cleanup_workdir():
        os.chdir(_orig_cwd)
        shutil.rmtree(workdir, ignore_errors=True)

    atexit.register(_cleanup_workdir)
    module = _load_plugin_module()
    cls = module.UniversalTTSPlugin

    ctx = _Context()
    cfg = _Config(
        {
            "enable": True,
            "engines": json.loads(json.dumps(SAMPLE_ENGINES)),
            "active_engine": "紫苏九月",
            "voice_clone_samples": [],
        }
    )
    inst = cls(ctx, cfg)

    print("\n[1] Web API 注册")
    check("注册 23 条路由", len(ctx.routes) == 23, f"实际 {len(ctx.routes)}")
    check(
        "路由均带插件名前缀",
        all(r.startswith(f"/{PKG}/") for r in ctx.routes),
        str(sorted(ctx.routes)),
    )
    for ep in [
        "engines/test",
        "samples",
        "samples/upload",
        "samples/delete",
        "engines/reorder",
        "bindings",
        "bindings/set",
        "bindings/delete",
        "bindings/clear",
        "config/export",
        "config/import",
        "history",
        "help",
    ]:
        check(f"包含 /{ep}", f"/{PKG}/{ep}" in ctx.routes)

    print("\n[2] 列表脱敏")
    payload = inst._build_engines_payload()
    e0, e1 = payload["engines"][0], payload["engines"][1]
    check("api_key 被清空", e0["api_key"] == "")
    check("_secrets 标记正确", e0["_secrets"] == ["api_key"], str(e0.get("_secrets")))
    check("空密钥不列入 _secrets", e1["_secrets"] == [])
    check("非敏感字段保留", e0["api_base"].endswith("/v1"))
    check("templates 已返回", len(payload["templates"]) == 10, str(len(payload["templates"])))

    print("\n[3] 字段类型转换")
    check("int 转换", inst._coerce_field("30", "int", 0) == 30)
    check("float 转换", inst._coerce_field("1.5", "float", 0.0) == 1.5)
    check("bool true", inst._coerce_field("true", "bool", False) is True)
    check("bool false", inst._coerce_field("false", "bool", True) is False)
    check("非法 int 回落默认", inst._coerce_field("abc", "int", 7) == 7)
    check("string 原样", inst._coerce_field(123, "string", "") == "123")

    print("\n[4] 敏感字段留空 = 不修改")
    normalized = inst._normalize_engine(
        "mimo_v2",
        {"instance_name": "备用", "api_key": "", "timeout": "45"},
        dict(cfg["engines"][1], api_key="EXISTING-KEY"),
    )
    check("原密钥保留", normalized["api_key"] == "EXISTING-KEY")
    check("timeout 已转 int", normalized["timeout"] == 45)
    check("__template_key 正确", normalized["__template_key"] == "mimo_v2")

    print("\n[5] index 校验")
    check("None → None", inst._as_index(None, 3) is None)
    check("字符串 2 → 2", inst._as_index("2", 3) == 2)
    for bad, label in [((5, 3), "越界"), (("abc", 3), "非数字")]:
        try:
            inst._as_index(*bad)
            check(f"{label}应报错", False)
        except ValueError:
            check(f"{label}应报错", True)

    print("\n[6] 复制引擎")
    web.request.payload = {"index": 0}
    res = asyncio.run(inst.web_clone_engine())
    check("返回成功", res.get("__ok__") is True, str(res)[:120])
    names = [e["instance_name"] for e in res["data"]["engines"]]
    check("副本插入源之后", names[1] == "紫苏九月 副本", str(names))
    check("总数为 3", len(names) == 3, str(names))
    check("配置文件已落盘", cfg.saved >= 1)

    print("\n[7] 副本重名递增")
    web.request.payload = {"index": 0}
    res = asyncio.run(inst.web_clone_engine())
    names = [e["instance_name"] for e in res["data"]["engines"]]
    check("第二次副本名带序号", "紫苏九月 副本2" in names, str(names))

    print("\n[8] 重名 / 空名校验")
    web.request.payload = {
        "index": None,
        "engine": {"__template_key": "mimo_v2", "instance_name": "备用"},
    }
    check("重名被拒绝", asyncio.run(inst.web_save_engine()).get("__ok__") is False)
    web.request.payload = {
        "index": None,
        "engine": {"__template_key": "mimo_v2", "instance_name": "   "},
    }
    check("空名被拒绝", asyncio.run(inst.web_save_engine()).get("__ok__") is False)

    print("\n[9] 删除默认引擎后默认顺延")
    cfg["engines"] = json.loads(json.dumps(SAMPLE_ENGINES))
    cfg["active_engine"] = "紫苏九月"
    inst2 = cls(_Context(), cfg)
    web.request.payload = {"index": 0}
    res = asyncio.run(inst2.web_delete_engine())
    check("删除成功", res.get("__ok__") is True, str(res)[:120])
    check("默认顺延", cfg["active_engine"] == "备用", repr(cfg["active_engine"]))

    print("\n[10] 设为默认")
    cfg["engines"] = json.loads(json.dumps(SAMPLE_ENGINES))
    inst3 = cls(_Context(), cfg)
    web.request.payload = {"name": "备用"}
    res = asyncio.run(inst3.web_activate_engine())
    check("激活成功", res.get("__ok__") is True)
    check("active_engine 已更新", cfg["active_engine"] == "备用")
    web.request.payload = {"name": "不存在"}
    check("未知引擎被拒绝", asyncio.run(inst3.web_activate_engine()).get("__ok__") is False)

    print("\n[11] 未知模板 / 旧版本降级")
    web.request.payload = {
        "index": None,
        "engine": {"__template_key": "no_such_tpl", "instance_name": "x"},
    }
    check("未知模板被拒绝", asyncio.run(inst3.web_save_engine()).get("__ok__") is False)
    inst_old = cls(type("OldCtx", (), {})(), _Config({"enable": True, "engines": []}))
    check("无 register_web_api 可实例化", inst_old is not None)
    check("空列表不报错", inst_old._build_engines_payload()["engines"] == [])

    print("\n[12] 新增引擎走 schema 默认值")
    web.request.payload = {
        "index": None,
        "engine": {"__template_key": "mimo_v2_5", "instance_name": "新引擎", "timeout": "99"},
    }
    before = len(cfg["engines"])
    res = asyncio.run(inst3.web_save_engine())
    check("新增成功", res.get("__ok__") is True, str(res)[:160])
    check("数量 +1", len(cfg["engines"]) == before + 1)
    new_engine = res["data"]["engines"][-1]
    check("timeout 转 int", new_engine["timeout"] == 99)
    check(
        "未提交字段回落 schema 默认值",
        new_engine["api_base"] == "https://api.xiaomimimo.com/v1",
        repr(new_engine.get("api_base")),
    )

    # ------------------------------------------------------------------
    # P1
    # ------------------------------------------------------------------

    print("\n[13] 试听：已保存引擎（提交 + 轮询）")
    STUB.update(audio=b"RIFF" + b"\x00" * 200, format="wav", fail=None)
    res = asyncio.run(_submit_and_wait(inst3, {"index": 0}, web))
    check("返回成功", res.get("__ok__") is True, str(res)[:160])
    data = res.get("data") or {}
    check("status 为 done", data.get("status") == "done", str(data.get("status")))
    check("audio 是 data URI", str(data.get("audio", "")).startswith("data:audio/wav;base64,"))
    check("format 正确", data.get("format") == "wav", str(data.get("format")))
    check("playable 为 True", data.get("playable") is True)
    check("bytes 正确", data.get("bytes") == 204, str(data.get("bytes")))
    check("took_ms 存在", isinstance(data.get("took_ms"), int))
    check("试听文本非空", data.get("text"))

    print("\n[14] 试听：未保存的草稿")
    res = asyncio.run(
        _submit_and_wait(
            inst3,
            {
                "index": 0,
                "engine": {
                    "__template_key": "mimo_v2_5_voiceclone",
                    "instance_name": "紫苏九月",
                    "api_base": "https://draft.example/v1",
                    "voice_sample_name": "draft.wav",
                },
            },
            web,
        )
    )
    check("草稿试听成功", (res.get("data") or {}).get("status") == "done", str(res)[:160])

    print("\n[15] 试听：参数与失败处理")
    check("缺参数被拒绝", asyncio.run(_submit_and_wait(inst3, {}, web)).get("__ok__") is False)
    check(
        "越界 index 被拒绝",
        asyncio.run(_submit_and_wait(inst3, {"index": 99}, web)).get("__ok__") is False,
    )
    web.request.query = {}
    check("缺 job_id 被拒绝", asyncio.run(inst3.web_test_result()).get("__ok__") is False)
    web.request.query = {"job_id": "不存在的任务"}
    check("未知任务被拒绝", asyncio.run(inst3.web_test_result()).get("__ok__") is False)

    STUB["fail"] = "provider rejected key"
    res = asyncio.run(_submit_and_wait(inst3, {"index": 0}, web))
    detail = (res.get("data") or {}).get("error") or ""
    check(
        "合成异常转为任务失败",
        (res.get("data") or {}).get("status") == "error" and "provider rejected key" in detail,
        str(res)[:160],
    )

    STUB["fail"] = "MiMo TTS 错误 [429]: Too many requests"
    res = asyncio.run(_submit_and_wait(inst3, {"index": 0}, web))
    detail = (res.get("data") or {}).get("error") or ""
    check("429 转成限流提示", "限流" in detail, detail[:120])
    STUB["fail"] = None

    STUB["audio"] = b"x" * (module.PREVIEW_MAX_BYTES + 1)
    res = asyncio.run(_submit_and_wait(inst3, {"index": 0}, web))
    check("超大音频转为任务失败", (res.get("data") or {}).get("status") == "error")
    STUB["audio"] = b"RIFF" + b"\x00" * 64

    print("\n[16] 样本列表")
    sdir = inst3._data_dir / module.SAMPLE_REL_DIR
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "存在.wav").write_bytes(b"wav")
    cfg["voice_clone_samples"] = [
        f"{module.SAMPLE_REL_DIR}/存在.wav",
        f"{module.SAMPLE_REL_DIR}/丢失.wav",
    ]
    res = asyncio.run(inst3.web_list_samples())
    samples = res["data"]["samples"]
    check("返回 2 条", len(samples) == 2, str(samples))
    check("存在的文件标记 exists", samples[0]["exists"] is True)
    check("缺失的文件标记 exists=False", samples[1]["exists"] is False)
    check("name 为纯文件名", samples[0]["name"] == "存在.wav", samples[0]["name"])

    print("\n[17] 上传样本")
    web.request.upload = _FakeUpload("新样本.wav", b"ID3" + b"\x00" * 50)
    res = asyncio.run(inst3.web_upload_sample())
    check("上传成功", res.get("__ok__") is True, str(res)[:160])
    check("落盘文件存在", (sdir / "新样本.wav").is_file())
    check("配置已追加", f"{module.SAMPLE_REL_DIR}/新样本.wav" in cfg["voice_clone_samples"],
          str(cfg["voice_clone_samples"]))
    check("返回 path 正确", res["data"]["path"] == f"{module.SAMPLE_REL_DIR}/新样本.wav")
    check("返回字节数", res["data"]["bytes"] == 53, str(res["data"].get("bytes")))

    print("\n[18] 上传重名自动加序号")
    web.request.upload = _FakeUpload("新样本.wav", b"another")
    res = asyncio.run(inst3.web_upload_sample())
    check("生成 _2 名称", res["data"]["name"] == "新样本_2.wav", str(res["data"].get("name")))
    check("原文件未被覆盖", (sdir / "新样本.wav").read_bytes() == b"ID3" + b"\x00" * 50)

    print("\n[19] 上传校验")
    web.request.upload = _FakeUpload("坏格式.txt", b"nope")
    check("非音频被拒绝", asyncio.run(inst3.web_upload_sample()).get("__ok__") is False)
    check("未覆盖非法文件", not (sdir / "坏格式.txt").exists())
    web.request.upload = None
    check("缺文件被拒绝", asyncio.run(inst3.web_upload_sample()).get("__ok__") is False)
    web.request.upload = _FakeUpload("../../escape.wav", b"x")
    res = asyncio.run(inst3.web_upload_sample())
    check("路径穿越被消毒", (sdir / "escape.wav").is_file(), str(res)[:160])
    check("未写到上级目录", not (Path(workdir).parent / "escape.wav").exists())

    print("\n[20] 删除样本")
    web.request.upload = None
    cfg["engines"][0]["voice_sample_name"] = "存在.wav"
    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/存在.wav"}
    res = asyncio.run(inst3.web_delete_sample())
    check("被引擎引用时拒绝", res.get("__ok__") is False, str(res)[:160])
    check("拒绝信息含引擎名", "紫苏九月" in res.get("error", ""), res.get("error"))
    check("文件仍存在", (sdir / "存在.wav").is_file())

    cfg["engines"][0]["voice_sample_name"] = ""
    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/存在.wav"}
    res = asyncio.run(inst3.web_delete_sample())
    check("无引用时删除成功", res.get("__ok__") is True, str(res)[:160])
    check("文件已删除", not (sdir / "存在.wav").exists())
    check("配置已移除", f"{module.SAMPLE_REL_DIR}/存在.wav" not in cfg["voice_clone_samples"])

    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/不存在.wav"}
    check("删除不存在的样本被拒绝", asyncio.run(inst3.web_delete_sample()).get("__ok__") is False)
    web.request.payload = {}
    check("缺 path 被拒绝", asyncio.run(inst3.web_delete_sample()).get("__ok__") is False)

    print("\n[21] 运行开关透出（全局 TTS / 插件开关）")
    ctx_on = _Context()
    ctx_on.astrbot_config = {"provider_tts_settings": {"enable": True}}
    inst_on = cls(ctx_on, _Config({"enable": True, "engines": json.loads(json.dumps(SAMPLE_ENGINES))}))
    p = inst_on._build_engines_payload()
    check("全局 TTS 开启 → True", p["global_tts_enabled"] is True, str(p.get("global_tts_enabled")))
    check("插件开关 → True", p["plugin_enabled"] is True)

    ctx_off = _Context()
    ctx_off.astrbot_config = {"provider_tts_settings": {"enable": False}}
    inst_off = cls(ctx_off, _Config({"enable": True, "engines": []}))
    p = inst_off._build_engines_payload()
    check("全局 TTS 关闭 → False", p["global_tts_enabled"] is False, str(p.get("global_tts_enabled")))

    inst_none = cls(_Context(), _Config({"enable": False, "engines": []}))
    p = inst_none._build_engines_payload()
    check("读不到开关 → None（不误报）", p["global_tts_enabled"] is None, str(p.get("global_tts_enabled")))
    check("插件开关关闭 → False", p["plugin_enabled"] is False)

    print("\n[22] 下载样本")
    (sdir / "下载.wav").write_bytes(b"RIFFdata")
    cfg["voice_clone_samples"] = [f"{module.SAMPLE_REL_DIR}/下载.wav"]
    web.request.query = {"path": f"{module.SAMPLE_REL_DIR}/下载.wav"}
    res = asyncio.run(inst3.web_download_sample())
    check("下载成功", res.get("__ok__") is True, str(res)[:160])
    body = res.get("data") or {}
    check("返回文件路径", str(body.get("__file__", "")).endswith("下载.wav"), str(body))
    check("下载文件名正确", body.get("filename") == "下载.wav", str(body.get("filename")))
    check(
        "Content-Type 为 audio/wav",
        body.get("content_type") == "audio/wav",
        str(body.get("content_type")),
    )
    web.request.query = {"path": f"{module.SAMPLE_REL_DIR}/不存在.wav"}
    check("下载不存在的样本被拒绝", asyncio.run(inst3.web_download_sample()).get("__ok__") is False)
    web.request.query = {}
    check("下载缺 path 被拒绝", asyncio.run(inst3.web_download_sample()).get("__ok__") is False)

    print("\n[23] 重命名样本")
    cfg["engines"][0]["voice_sample_name"] = "下载.wav"
    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/下载.wav", "new_name": "改名后.wav"}
    res = asyncio.run(inst3.web_rename_sample())
    check("重命名成功", res.get("__ok__") is True, str(res)[:160])
    check("新文件存在", (sdir / "改名后.wav").is_file())
    check("旧文件已消失", not (sdir / "下载.wav").exists())
    check(
        "配置已更新",
        f"{module.SAMPLE_REL_DIR}/改名后.wav" in cfg["voice_clone_samples"],
        str(cfg["voice_clone_samples"]),
    )
    check(
        "引擎引用已同步",
        cfg["engines"][0]["voice_sample_name"] == "改名后.wav",
        str(cfg["engines"][0].get("voice_sample_name")),
    )
    check(
        "返回同步数量",
        res["data"].get("renamed_refs") == 1,
        str(res["data"].get("renamed_refs")),
    )

    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/改名后.wav", "new_name": "坏格式.txt"}
    check("非法扩展名被拒绝", asyncio.run(inst3.web_rename_sample()).get("__ok__") is False)
    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/改名后.wav", "new_name": "  "}
    check("空文件名被拒绝", asyncio.run(inst3.web_rename_sample()).get("__ok__") is False)
    web.request.payload = {
        "path": f"{module.SAMPLE_REL_DIR}/改名后.wav",
        "new_name": "../../逃逸.wav",
    }
    res = asyncio.run(inst3.web_rename_sample())
    check("路径穿越被消毒", (sdir / "逃逸.wav").is_file(), str(res)[:160])
    check("未写到上级目录", not (Path(workdir).parent / "逃逸.wav").exists())

    (sdir / "占位.wav").write_bytes(b"x")
    cfg["voice_clone_samples"].append(f"{module.SAMPLE_REL_DIR}/占位.wav")
    web.request.payload = {"path": f"{module.SAMPLE_REL_DIR}/逃逸.wav", "new_name": "占位.wav"}
    check("重名被拒绝", asyncio.run(inst3.web_rename_sample()).get("__ok__") is False)

    print("\n[24] 全局设置：读取")
    cfg2 = _Config(
        {
            "enable": True,
            "engines": [],
            "tts_probability": 80,
            "admin_user_ids": ["1", "2"],
        }
    )
    inst4 = cls(_Context(), cfg2)
    res = asyncio.run(inst4.web_get_settings())
    check("读取成功", res.get("__ok__") is True, str(res)[:160])
    payload = res.get("data") or {}
    fields = {f["key"]: f for f in payload.get("fields", [])}
    check("返回 11 个字段", len(fields) == 11, str(sorted(fields)))
    check("含智能跳过字段", "tts_skip_code_blocks" in fields and "tts_skip_patterns" in fields)
    check("不含 engines", "engines" not in fields)
    check("不含 active_engine", "active_engine" not in fields)
    check("不含 voice_clone_samples", "voice_clone_samples" not in fields)
    check("读取到概率值", fields["tts_probability"]["value"] == 80, str(fields["tts_probability"]))
    check("list 类型透出", fields["admin_user_ids"]["type"] == "list")
    check("bool 类型透出", fields["enable"]["type"] == "bool")
    check("无敏感字段", payload.get("secrets") == [], str(payload.get("secrets")))

    print("\n[25] 全局设置：保存")
    web.request.payload = {
        "values": {
            "tts_probability": "55",
            "admin_user_ids": "a\nb\nc",
            "enable": False,
        }
    }
    res = asyncio.run(inst4.web_save_settings())
    check("保存成功", res.get("__ok__") is True, str(res)[:160])
    check("int 已写入", cfg2.get("tts_probability") == 55, str(cfg2.get("tts_probability")))
    check(
        "list 按行拆分",
        cfg2.get("admin_user_ids") == ["a", "b", "c"],
        str(cfg2.get("admin_user_ids")),
    )
    check("bool 已写入", cfg2.get("enable") is False, str(cfg2.get("enable")))
    check("配置已持久化", cfg2.saved > 0)

    web.request.payload = {"values": {"tts_probability": "abc"}}
    check("非法数字被拒绝", asyncio.run(inst4.web_save_settings()).get("__ok__") is False)
    web.request.payload = {"values": {}}
    check("空改动被拒绝", asyncio.run(inst4.web_save_settings()).get("__ok__") is False)
    web.request.payload = {}
    check("缺 values 被拒绝", asyncio.run(inst4.web_save_settings()).get("__ok__") is False)

    print("\n[26] vbinds")
    inst._session_bindings = {}
    payload = inst._build_engines_payload()
    token = payload.get("token")
    check("引擎负载带 token", bool(token), repr(token))
    check("设置负载带 token", bool(inst._build_settings_payload().get("token")))
    check("绑定负载带 token", bool(inst._bindings_payload().get("token")))
    check(
        "token 只含十六进制",
        all(c in "0123456789abcdef" for c in str(token)),
        repr(token),
    )

    web.request.payload = {"engine": dict(payload["engines"][0]), "token": "deadbeefdeadbeef"}
    res = asyncio.run(inst.web_save_engine())
    check("过期指纹被拒绝", res.get("__ok__") is False, str(res)[:160])
    check("拒绝信息提示刷新", "刷新" in str(res.get("error") or ""), str(res.get("error")))

    web.request.payload = {"engine": dict(payload["engines"][0]), "index": 0, "token": token}
    check("正确指纹可保存", asyncio.run(inst.web_save_engine()).get("__ok__") is True)

    web.request.payload = {"index": 0}
    check("未带指纹不拦截", asyncio.run(inst.web_clone_engine()).get("__ok__") is True)
    # 还原引擎列表，避免复制出来的副本影响后续断言
    inst.config["engines"] = json.loads(json.dumps(SAMPLE_ENGINES))
    asyncio.run(inst._persist_config())

    print("\n[27] 会话绑定")
    inst._session_bindings = {}
    web.request.payload = {}
    res = asyncio.run(inst.web_list_bindings())
    data = res.get("data") or {}
    check("空绑定列表", res.get("__ok__") is True and data.get("bindings") == [])
    check("可选引擎覆盖全部实例", len(data.get("engines") or []) == len(inst._list_engines()))
    check("暴露 disabled_engine", bool(data.get("disabled_engine")))

    sid = "qq:GroupMessage:12345"
    target = SAMPLE_ENGINES[1]["instance_name"]
    web.request.payload = {"session_id": sid, "target": target}
    res = asyncio.run(inst.web_set_binding())
    entries = (res.get("data") or {}).get("bindings") or []
    check("设置绑定成功", res.get("__ok__") is True, str(res)[:160])
    check("列表含新绑定", len(entries) == 1 and entries[0]["session_id"] == sid, str(entries))
    check("绑定目标正确", entries[0]["target"] == target)
    check("exists 为真", entries[0]["exists"] is True)
    check("显示名可读", bool(entries[0]["display"]) and entries[0]["display"] != sid, entries[0]["display"])
    check("绑定已落盘", inst._bindings_file.exists())

    web.request.payload = {"session_id": sid, "target": "不存在的引擎"}
    check("绑定不存在引擎被拒绝", asyncio.run(inst.web_set_binding()).get("__ok__") is False)

    disabled = inst._bindings_payload()["disabled_engine"]
    web.request.payload = {"session_id": sid, "target": disabled}
    res = asyncio.run(inst.web_set_binding())
    check(
        "可绑定为不合成",
        ((res.get("data") or {}).get("bindings") or [{}])[0].get("target") == disabled,
        str(res.get("data"))[:160],
    )

    web.request.payload = {"session_id": sid, "target": ""}
    res = asyncio.run(inst.web_set_binding())
    check(
        "空目标等于解绑",
        all(b["session_id"] != sid for b in (res.get("data") or {}).get("bindings") or []),
    )

    web.request.payload = {"session_id": ""}
    check("缺 session_id 被拒绝", asyncio.run(inst.web_delete_binding()).get("__ok__") is False)

    inst._session_bindings[sid] = "已被删除的引擎"
    entries = inst._binding_entries()
    check("悬空绑定标记 exists=False", entries[0]["exists"] is False, str(entries[0]))

    web.request.payload = {"session_id": sid}
    res = asyncio.run(inst.web_delete_binding())
    check("解除绑定成功", res.get("__ok__") is True and (res.get("data") or {}).get("bindings") == [])

    inst._session_bindings = {"a:GroupMessage:1": target, "b:FriendMessage:2": target}
    web.request.payload = {}
    res = asyncio.run(inst.web_clear_bindings())
    check("清空绑定成功", res.get("__ok__") is True and (res.get("data") or {}).get("bindings") == [])
    check("内存已清空", inst._session_bindings == {})
    check("空绑定再清空被拒绝", asyncio.run(inst.web_clear_bindings()).get("__ok__") is False)

    print("\n[28] 引擎排序")
    before = [e["instance_name"] for e in inst._list_engines()]
    check("至少两个引擎", len(before) >= 2, str(before))
    web.request.payload = {"order": list(range(len(before)))[::-1], "token": inst._config_token()}
    res = asyncio.run(inst.web_reorder_engines())
    after = [e["instance_name"] for e in inst._list_engines()]
    check("排序成功", res.get("__ok__") is True, str(res)[:160])
    check("顺序已反转", after == before[::-1], f"{before} -> {after}")

    web.request.payload = {"order": [0] * len(before), "token": inst._config_token()}
    check("重复下标被拒绝", asyncio.run(inst.web_reorder_engines()).get("__ok__") is False)
    web.request.payload = {"order": [0], "token": inst._config_token()}
    check("数量不符被拒绝", asyncio.run(inst.web_reorder_engines()).get("__ok__") is False)
    web.request.payload = {"order": [0, 99], "token": inst._config_token()}
    check("下标越界被拒绝", asyncio.run(inst.web_reorder_engines()).get("__ok__") is False)
    web.request.payload = {"order": [0, 1], "token": "0000000000000000"}
    check("排序也校验指纹", asyncio.run(inst.web_reorder_engines()).get("__ok__") is False)

    print("\n[29] 配置导出 / 导入")
    web.request.query = {}
    res = asyncio.run(inst.web_export_config())
    data = res.get("data") or {}
    exported = data.get("config") or {}
    check("导出成功", res.get("__ok__") is True, str(res)[:160])
    check("默认脱敏", _sensitive_all_empty(exported.get("engines") or []), str(exported.get("engines"))[:120])
    check("带版本与时间", bool(data.get("plugin_version")) and bool(data.get("exported_at")))
    check("标记未含密钥", data.get("include_secrets") is False)

    web.request.query = {"include_secrets": "1"}
    res = asyncio.run(inst.web_export_config())
    data = res.get("data") or {}
    raw_engines = (data.get("config") or {}).get("engines") or []
    check("显式要求时含密钥", not _sensitive_all_empty(raw_engines), str(raw_engines)[:120])
    check("标记已含密钥", data.get("include_secrets") is True)

    web.request.query = {}
    web.request.payload = {}
    check("缺 config 被拒绝", asyncio.run(inst.web_import_config()).get("__ok__") is False)

    web.request.payload = {"config": {"tts_probability": "abc"}}
    check("非法数字被拒绝", asyncio.run(inst.web_import_config()).get("__ok__") is False)

    web.request.payload = {"config": {"tts_probability": 42, "enable": False}}
    res = asyncio.run(inst.web_import_config())
    check("导入成功", res.get("__ok__") is True, str(res)[:160])
    check("int 已写入", inst.config.get("tts_probability") == 42, repr(inst.config.get("tts_probability")))
    check("bool 已写入", inst.config.get("enable") is False)
    check(
        "返回已导入字段",
        "tts_probability" in ((res.get("data") or {}).get("imported") or []),
        str((res.get("data") or {}).get("imported")),
    )

    web.request.payload = {"config": {"engines": [{"instance_name": "无模板"}]}}
    check("引擎缺 __template_key 被拒绝", asyncio.run(inst.web_import_config()).get("__ok__") is False)
    web.request.payload = {"config": {"unknown_key": 1}}
    check("无匹配字段被拒绝", asyncio.run(inst.web_import_config()).get("__ok__") is False)

    print("\n[30] 变更历史")
    web.request.query = {"limit": "30"}
    res = asyncio.run(inst.web_list_history())
    entries = (res.get("data") or {}).get("entries") or []
    check("历史查询成功", res.get("__ok__") is True, str(res)[:160])
    check("已有历史记录", len(entries) > 0, str(len(entries)))
    actions = {e.get("action") for e in entries}
    check("记录了排序", "engines.reorder" in actions, str(sorted(actions)))
    check("记录了绑定", any(str(a).startswith("bindings.") for a in actions), str(sorted(actions)))
    check("记录了导入", "config.import" in actions, str(sorted(actions)))
    check("记录了导出", "config.export" in actions, str(sorted(actions)))
    check("含时间字段", bool(entries[0].get("time")), str(entries[0]))
    stamps = [e.get("ts") or 0 for e in entries]
    check("新的在前", stamps == sorted(stamps, reverse=True), str(stamps[:5]))

    web.request.query = {"limit": "2"}
    res = asyncio.run(inst.web_list_history())
    check("limit 生效", len((res.get("data") or {}).get("entries") or []) == 2)
    web.request.query = {"limit": "abc"}
    res = asyncio.run(inst.web_list_history())
    check("非法 limit 回落", res.get("__ok__") is True)

    print("\n[31] 状态总览")
    payload = inst._build_engines_payload()
    status = payload.get("status") or {}
    check("含引擎数", status.get("engine_count") == len(inst._list_engines()), str(status))
    check("含绑定数", status.get("binding_count") == len(inst._session_bindings), str(status))
    check("含样本数", isinstance(status.get("sample_count"), int), str(status))
    check("含默认引擎", "active_engine" in status, str(status))
    inst.config["active_engine"] = "不存在的引擎"
    status = inst._build_engines_payload().get("status") or {}
    check("默认引擎缺失被标记", status.get("active_engine_exists") is False, str(status))
    inst.config["active_engine"] = SAMPLE_ENGINES[0]["instance_name"]
    status = inst._build_engines_payload().get("status") or {}
    check("默认引擎存在被标记", status.get("active_engine_exists") is True, str(status))

    print("\n[32] 指令与帮助")
    specs = module.COMMAND_SPECS
    check("规格表非空", isinstance(specs, list) and len(specs) >= 14, str(len(specs)))

    # 规格表必须与实际注册的指令严格一致（防止新增指令忘了写帮助）
    import re as _re

    source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    decorated = sorted(_re.findall(r'@filter\.command\("([^"]+)"\)', source))
    spec_names = sorted(spec["name"] for spec in specs)
    check("规格表与实际指令一致", decorated == spec_names, f"{decorated} vs {spec_names}")
    check("指令数量为 14", len(decorated) == 14, str(decorated))

    missing_fields = [
        spec.get("name")
        for spec in specs
        if not spec.get("usage") or not spec.get("summary") or "admin" not in spec
    ]
    check("每条都有 usage/summary/admin", not missing_fields, str(missing_fields))
    check(
        "usage 以 / 开头",
        all(str(spec["usage"]).startswith("/") for spec in specs),
        str([spec["usage"] for spec in specs])[:160],
    )
    check(
        "usage 里含命令名",
        all(spec["name"] in spec["usage"] for spec in specs),
        str([(s["name"], s["usage"]) for s in specs if s["name"] not in s["usage"]]),
    )
    check(
        "列表字段为字符串数组",
        all(
            isinstance(spec.get("examples", []), list)
            and isinstance(spec.get("notes", []), list)
            for spec in specs
        ),
    )
    admin_count = sum(1 for spec in specs if spec["admin"])
    check("管理员指令 8 条", admin_count == 8, str(admin_count))

    web.request.query = {}
    res = asyncio.run(inst.web_get_help())
    data = res.get("data") or {}
    check("帮助接口可用", res.get("__ok__") is True, str(res)[:160])
    check("返回全部指令", len(data.get("commands") or []) == len(specs))
    snap = data.get("snapshot") or {}
    for field in (
        "plugin_enabled",
        "global_tts_enabled",
        "runtime_enabled",
        "active_engine",
        "engine_count",
        "binding_count",
        "text_limit",
        "text_limit_enabled",
        "admin_only",
    ):
        check(f"快照含 {field}", field in snap, str(sorted(snap)))

    text = inst._render_help_text()
    check("文本帮助非空", isinstance(text, str) and len(text) > 100, str(len(text)))
    check("文本含全部指令", all(spec["name"] in text for spec in specs))
    check("文本含分组标题", "普通指令" in text and "管理员指令" in text)
    check("文本含斜杠提示", "/" in text and "开头" in text)

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    meta_help = str(meta.get("help") or "")
    check("metadata 含 help", bool(meta_help), repr(meta_help)[:80])
    check(
        "metadata help 覆盖全部指令",
        all(f"/{spec['name']}" in meta_help for spec in specs),
        str([s["name"] for s in specs if f"/{s['name']}" not in meta_help]),
    )

    # support_platforms 必须是经源码核实的白名单
    platforms = meta.get("support_platforms")
    check("metadata 含 support_platforms", isinstance(platforms, list), repr(platforms)[:80])
    check("平台数量为 14", len(platforms) == 14, str(len(platforms)))
    check("平台无重复", len(set(platforms)) == len(platforms), str(platforms))
    check("包含 aiocqhttp", "aiocqhttp" in platforms)
    KNOWN_IDS = {
        "aiocqhttp", "qq_official", "qq_official_webhook", "telegram", "discord",
        "kook", "lark", "line", "mattermost", "misskey", "satori", "slack",
        "webchat", "wecom", "wecom_ai_bot", "weixin_oc",
        "weixin_official_account", "dingtalk",
    }
    unknown_ids = [p for p in platforms if p not in KNOWN_IDS]
    check("平台 ID 均为适配器真实 ID", not unknown_ids, str(unknown_ids))
    check(
        "未声明无 Record 处理的 qq_official_webhook",
        "qq_official_webhook" not in platforms,
    )
    check(
        "描述未再声称注册 Provider",
        "TTS Provider" not in str(meta.get("desc") or ""),
        str(meta.get("desc"))[:120],
    )

    print("\n[33] 合成统计")

    class _StatsEngine:
        """独立 stub 引擎：不依赖前面用例遗留的全局引擎状态。"""

        instance_name = "紫苏九月"

        async def synthesize(self, text):
            if STUB["fail"]:
                raise RuntimeError(STUB["fail"])
            return STUB["audio"], STUB["format"]

    stats_engine = _StatsEngine()
    inst._engine_stats.clear()
    summary = inst._engine_stats_summary(stats_engine.instance_name)
    check("无数据时返回 None", summary is None)
    audio, fmt = asyncio.run(inst._synthesize_with_stats(stats_engine, "统计-成功"))
    check("成功路径返回合成结果", audio == STUB["audio"] and fmt == STUB["format"])
    summary = inst._engine_stats_summary(stats_engine.instance_name)
    check("成功计数为 1", summary and summary["success"] == 1, str(summary))
    check("平均耗时字段存在", summary and summary["avg_ms"] is not None, str(summary))
    check("最近成功时间已记录", summary and summary["last_success_at"] > 0)

    STUB["fail"] = "provider rejected key"
    raised = False
    try:
        asyncio.run(inst._synthesize_with_stats(stats_engine, "统计-失败"))
    except RuntimeError:
        raised = True
    check("异常原样抛出", raised)
    STUB["fail"] = None
    summary = inst._engine_stats_summary(stats_engine.instance_name)
    check("失败计数为 1", summary and summary["fail"] == 1, str(summary))
    check("最近错误已记录", summary and "provider rejected key" in summary["last_error"], str(summary))
    check("成功计数不受失败影响", summary and summary["success"] == 1)

    payload = inst._build_engines_payload()
    named_item = next(
        (e for e in payload["engines"] if e.get("instance_name") == stats_engine.instance_name),
        None,
    )
    check("引擎 payload 带 stats", named_item is not None and named_item.get("stats") is not None,
          str((named_item or {}).get("stats")))
    if named_item:
        check("payload 统计数字一致",
              named_item["stats"]["success"] == 1 and named_item["stats"]["fail"] == 1,
              str(named_item["stats"]))
    other_items = [e for e in payload["engines"] if e.get("instance_name") != stats_engine.instance_name]
    check("无数据引擎 stats 为 None", all(e.get("stats") is None for e in other_items),
          str([e.get("stats") for e in other_items]))

    print("\n[34] 智能跳过")
    inst.config["tts_skip_code_blocks"] = True
    inst.config["tts_skip_patterns"] = []
    check("代码块被跳过", inst._should_skip_tts("看这段代码\n```py\nprint(1)\n```") == "code_block")
    check("普通文本不跳过", inst._should_skip_tts("你好，今天天气不错。") == "")
    check("普通链接不跳过",
          inst._should_skip_tts("参考 https://example.com/a 这个文档写得很详细") == "")
    check("纯长链接被跳过",
          inst._should_skip_tts("https://example.com/very/long/path/segment/xyz123?query=abcdefg")
          == "url_only")
    check("短链接+正文不跳过",
          inst._should_skip_tts("看 https://a.bc 这条新闻，讲得很有道理") == "")
    inst.config["tts_skip_patterns"] = [r"^\d{4,8}$"]
    check("自定义正则命中", inst._should_skip_tts("123456") == r"pattern:^\d{4,8}$",
          inst._should_skip_tts("123456"))
    check("自定义正则不误伤", inst._should_skip_tts("订单号 123456 已发货") == "")
    inst.config["tts_skip_patterns"] = ["([无效正则"]
    check("无效正则被忽略", inst._should_skip_tts("普通文本") == "")
    check("无效正则有告警", "([无效正则" in inst._warned_skip_patterns)
    inst.config["tts_skip_code_blocks"] = False
    check("关闭后代码块不跳过", inst._should_skip_tts("```py\nx\n```") == "")
    inst.config["tts_skip_code_blocks"] = True
    inst.config["tts_skip_patterns"] = []
    schema = inst._read_schema()
    check("schema 含 tts_skip_code_blocks", "tts_skip_code_blocks" in schema)
    check("schema 含 tts_skip_patterns", "tts_skip_patterns" in schema)
    check("schema 默认值正确",
          schema["tts_skip_code_blocks"].get("default") is True
          and schema["tts_skip_patterns"].get("default") == [])

    print("\n[35] 绑定导出导入")
    DISABLED = module.DISABLED_ENGINE
    inst._session_bindings = {
        "aiocqhttp:GroupMessage:111": "紫苏九月",
        "webchat:FriendMessage:222": DISABLED,
    }
    web.request.query = {}
    res = asyncio.run(inst.web_export_config())
    exported_bindings = ((res.get("data") or {}).get("config") or {}).get("session_bindings")
    check("导出含会话绑定", isinstance(exported_bindings, list) and len(exported_bindings) == 2,
          str(exported_bindings))
    check("绑定含 session_id 与 engine",
          all({"session_id", "engine"} <= set(item) for item in exported_bindings or []),
          str(exported_bindings))

    good_payload = {
        "config": {
            "engines": json.loads(json.dumps(SAMPLE_ENGINES)),
            "session_bindings": [
                {"session_id": "s1", "engine": "备用"},
                {"session_id": "s2", "engine": DISABLED},
            ],
        }
    }
    web.request.payload = good_payload
    res = asyncio.run(inst.web_import_config())
    check("绑定导入成功", res.get("__ok__") is True, str(res)[:160])
    check("返回替换数量", (res.get("data") or {}).get("imported_bindings") == 2)
    check("绑定已整体替换", inst._session_bindings == {"s1": "备用", "s2": DISABLED},
          str(inst._session_bindings))

    bad_payload = {
        "config": {
            "engines": json.loads(json.dumps(SAMPLE_ENGINES)),
            "session_bindings": [{"session_id": "s1", "engine": "不存在的引擎"}],
        }
    }
    web.request.payload = bad_payload
    res = asyncio.run(inst.web_import_config())
    check("引用不存在引擎被拒绝", res.get("__ok__") is False, str(res)[:160])
    check("拒绝时绑定未被改动", inst._session_bindings == {"s1": "备用", "s2": DISABLED})

    bad_shape = {
        "config": {
            "engines": json.loads(json.dumps(SAMPLE_ENGINES)),
            "session_bindings": [{"engine": "备用"}],
        }
    }
    web.request.payload = bad_shape
    res = asyncio.run(inst.web_import_config())
    check("缺 session_id 被拒绝", res.get("__ok__") is False, str(res)[:160])

    not_list = {
        "config": {
            "engines": json.loads(json.dumps(SAMPLE_ENGINES)),
            "session_bindings": "nope",
        }
    }
    web.request.payload = not_list
    res = asyncio.run(inst.web_import_config())
    check("非列表绑定被拒绝", res.get("__ok__") is False, str(res)[:160])

    web.request.payload = {"config": {"engines": json.loads(json.dumps(SAMPLE_ENGINES))}}
    res = asyncio.run(inst.web_import_config())
    check("不含绑定字段时不改动绑定", res.get("__ok__") is True
          and (res.get("data") or {}).get("imported_bindings") == 0, str(res)[:160])
    check("旧版导出文件兼容", inst._session_bindings == {"s1": "备用", "s2": DISABLED})

    print("\n[36] 近期活跃")
    inst._session_bindings = {}
    inst._session_activity = {}
    inst._last_activity_save_at = 0  # 绕过节流，保证断言可落盘
    res = asyncio.run(inst.web_list_bindings())
    check("空活跃列表", res.get("__ok__") is True
          and (res.get("data") or {}).get("activity") == [])
    check("payload 含 active_engine", "active_engine" in (res.get("data") or {}),
          str(sorted((res.get("data") or {}))))

    inst._record_activity("a:GroupMessage:1", "紫苏九月")
    inst._record_activity("b:FriendMessage:2", "备用")
    entry = inst._session_activity["a:GroupMessage:1"]
    check("累计 1 次", entry["count"] == 1, str(entry))
    check("引擎已记录", entry["engine"] == "紫苏九月", str(entry))
    inst._record_activity("a:GroupMessage:1", "紫苏九月")
    check("重复记录累计", inst._session_activity["a:GroupMessage:1"]["count"] == 2)
    check("活跃已落盘", inst._activity_file.exists())

    res = asyncio.run(inst.web_list_bindings())
    activity = (res.get("data") or {}).get("activity") or []
    check("两个活跃会话", len(activity) == 2, str(activity))
    check("键齐全", all(
        {"session_id", "display", "engine", "last_at", "count", "binding"} <= set(item)
        for item in activity
    ), str(activity)[:200])
    check("未绑定时标注为空", all(item["binding"] == "" for item in activity), str(activity)[:200])
    check("后活跃的排前面", activity[0]["session_id"] == "a:GroupMessage:1",
          str([a["session_id"] for a in activity]))
    check("显示名可读", bool(activity[0]["display"]) and activity[0]["display"] != activity[0]["session_id"])
    check("最近时间戳有效", activity[0]["last_at"] > 0)

    # 已绑定的会话保留在活跃列表，但带状态标注（前端据此筛选视图）。
    inst._session_bindings = {"a:GroupMessage:1": "备用"}
    res = asyncio.run(inst.web_list_bindings())
    activity = (res.get("data") or {}).get("activity") or []
    annotated = {item["session_id"]: item["binding"] for item in activity}
    check("已绑定的带状态标注", annotated.get("a:GroupMessage:1") == "备用", str(annotated))
    check("未绑定的标注为空", annotated.get("b:FriendMessage:2") == "", str(annotated))
    inst._session_bindings = {}

    disabled = inst._bindings_payload()["disabled_engine"]
    inst._session_bindings = {"a:GroupMessage:1": disabled}
    activity = inst._activity_entries()
    check("禁音绑定的标注", activity[0]["binding"] == disabled, str(activity))
    inst._session_bindings = {}

    # 转正后进入显式绑定列表，活跃条目同步带绑定标注。
    web.request.payload = {"session_id": "b:FriendMessage:2", "target": "备用"}
    res = asyncio.run(inst.web_set_binding())
    data = res.get("data") or {}
    activity = data.get("activity") or []
    check("转正后进入显式绑定",
          any(b["session_id"] == "b:FriendMessage:2" for b in data.get("bindings") or []),
          str(data.get("bindings"))[:200])
    turned = next((a for a in activity if a["session_id"] == "b:FriendMessage:2"), {})
    check("转正后活跃条目标注绑定", turned.get("binding") == "备用", str(activity))
    inst._session_bindings = {}

    # 持久化往返。
    inst._save_activity(force=True)
    inst2_activity = dict(inst._session_activity)
    inst._session_activity = {}
    inst._load_activity()
    check("落盘后可恢复", inst._session_activity == inst2_activity,
          f"{inst._session_activity} vs {inst2_activity}")
    # 超期与坏数据被丢弃。
    inst._session_activity["old:GroupMessage:9"] = {
        "last_at": __import__("time").time() - module.UniversalTTSPlugin.ACTIVITY_RETENTION_SECONDS - 1,
        "engine": "紫苏九月",
        "count": 3,
    }
    inst._session_activity["bad:GroupMessage:9"] = {"engine": "紫苏九月"}
    inst._session_activity["junk"] = "not-a-dict"
    inst._save_activity(force=True)
    inst._session_activity = {}
    inst._load_activity()
    check("超期条目被丢弃", "old:GroupMessage:9" not in inst._session_activity)
    check("坏条目被丢弃",
          "bad:GroupMessage:9" not in inst._session_activity and "junk" not in inst._session_activity)
    # 损坏的 JSON 不崩。
    inst._activity_file.write_text("{oops", encoding="utf-8")
    inst._session_activity = {"x": {"last_at": 1.0, "engine": "e", "count": 1}}
    inst._load_activity()
    check("损坏文件被清空", inst._session_activity == {})

    # /tts_test 也计入活跃：两个写入口径（自动合成 + 试听）都要接线。
    check("tts_test 记录活跃已接线",
          source.count("self._record_activity(session_id, engine.instance_name)") >= 2,
          str(source.count("self._record_activity(session_id, engine.instance_name)")))

    print("\n[37] 核心 TTS 抑制")

    class _FakeCT:
        LLM_RESULT = "llm"
        GENERAL_RESULT = "general"

    class _FakeResult:
        def __init__(self, ct):
            self.result_content_type = ct

        def set_result_content_type(self, t):
            self.result_content_type = t

    orig_ct = getattr(module, "ResultContentType", None)
    orig_ctx_cfg = getattr(inst.context, "astrbot_config", None)
    try:
        module.ResultContentType = _FakeCT
        inst.context.astrbot_config = {}

        res = _FakeResult(_FakeCT.LLM_RESULT)
        inst._suppress_core_tts(res)
        check("LLM 结果被标记为普通结果", res.result_content_type == _FakeCT.GENERAL_RESULT,
              str(res.result_content_type))

        res = _FakeResult(_FakeCT.GENERAL_RESULT)
        inst._suppress_core_tts(res)
        check("已是普通结果则不动", res.result_content_type == _FakeCT.GENERAL_RESULT)

        inst.context.astrbot_config = {"content_safety": {"content_safe_check_reply": True}}
        res = _FakeResult(_FakeCT.LLM_RESULT)
        inst._suppress_core_tts(res)
        check("内容安全回复检查开启时不抑制", res.result_content_type == _FakeCT.LLM_RESULT,
              str(res.result_content_type))
        inst.context.astrbot_config = {}

        module.ResultContentType = None
        res = _FakeResult(_FakeCT.LLM_RESULT)
        inst._suppress_core_tts(res)
        check("枚举缺失时安全降级", res.result_content_type == _FakeCT.LLM_RESULT)
        module.ResultContentType = _FakeCT

        res = _FakeResult(None)
        inst._suppress_core_tts(res)
        check("异常对象不崩", res.result_content_type is None)
    finally:
        module.ResultContentType = orig_ct
        if orig_ctx_cfg is None:
            if hasattr(inst.context, "astrbot_config"):
                delattr(inst.context, "astrbot_config")
        else:
            inst.context.astrbot_config = orig_ctx_cfg

    check("抑制已接入全部跳过点", source.count("self._suppress_core_tts(result)") >= 6,
          str(source.count("self._suppress_core_tts(result)")))
    check("枚举带回退导入", "ResultContentType = None" in source)

    print()
    if failures:
        print(f"=== {len(failures)} 项失败 ===")
        for f in failures:
            print("  -", f)
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
