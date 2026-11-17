# Universal TTS / 通用 TTS

本仓库是 [CyrilPeng/astrbot_plugin_universal_tts](https://github.com/CyrilPeng/astrbot_plugin_universal_tts) 的分支，面向 AstrBot 的通用语音合成插件。支持多引擎热切换、会话级绑定、WebUI 管理，以及 OpenAI 兼容接口的两步式音色克隆。

> 原项目：<https://github.com/CyrilPeng/astrbot_plugin_universal_tts>

---

## 支持的引擎

| 提供商 | 引擎 | 说明 |
|---|---|---|
| **OpenAI** | 兼容克隆 | 上传样本 → 保存音色 → 合成，适配 Unsloth Studio / audio.cpp 等两步式接口 |
| **OpenAI** | 兼容 TTS | 标准 `/v1/audio/speech` 接口 |
| **MiMo** | V2.5-TTS | 预置音色 + 风格控制，支持唱歌模式 |
| **MiMo** | V2.5-VoiceDesign | 文本描述生成音色 |
| **MiMo** | V2.5-VoiceClone | 音频样本克隆音色 |
| **火山引擎** | SAMI TTS | 字节跳动发音人 |
| **阿里云百炼** | CosyVoice | DashScope 兼容接口 |
| **Azure** | Cognitive Services | SSML + 情感风格 |
| **ElevenLabs** | Text-to-Speech | 多语言高保真语音 |
| **Unsloth** | 歌曲/音乐 | 调用 `/v1/audio/run`，按 `[[MUSIC]]` + JSON 生成歌曲/配乐 |
| **自定义** | HTTP TTS | 模板化接入任意 HTTP TTS API |

---

## OpenAI 兼容音色克隆

适用于在标准 OpenAI 语音接口之外，额外提供「上传音频输入」和「保存音色」路由的服务，例如 **Unsloth Studio / audio.cpp（CosyVoice3 GGUF）**：

```text
POST {api_base}/audio/inputs?name=<文件名>   原始音频字节  -> {"id": <input_id>}
POST {api_base}/audio/voices                 {"name", "source": {"input_id"}} -> {"id": <voice_id>}
POST {api_base}/audio/speech                 {"model", "voice", "input", "response_format"} -> 音频二进制
```

### 功能

- **自动注册音色**：读取插件里上传的样本，调用 `inputs` 上传、`voices` 创建保存音色，再调用 `speech` 合成；之后复用 `voice_id`
- **保存时预注册**：在引擎编辑页保存时就完成上传样本和创建音色，对话框显示上传/创建进度条；首次合成不需要再等待注册
- **音色名取样本文件名**：例如 `me.wav` 创建的保存音色就叫 `me`
- **音色缓存落盘**：缓存写在 `data/plugin_data/astrbot_plugin_universal_tts/openai_clone_voices.json`，重启后优先复用；样本内容或服务地址变化时自动重建
- **失效自愈**：服务端重启或删除音色后，合成返回音色相关错误时自动重新上传样本并重建一次
- **已有音色下拉选择**：`voice_id` 是下拉框，打开编辑器时自动调用 `GET /audio/voices` 加载服务端音色并按名称展示；留空表示用样本自动创建
- **四种克隆模式**：Same language / Cross-lingual / Instruct / None
- **生成语言可选**：`language` 默认 `auto`，不发送语言提示；可选择 Chinese / Japanese 等
- **随机种子可选**：`seed` 留空不发送；填写整数时作为 `seed` 参数传入
- **模型名可选**：`model` 留空不发送，由服务端使用当前加载的模型；填写后原样传入

### 四种克隆模式

| 模式 | 界面 | 请求体 |
|---|---|---|
| **Same language**（默认） | 显示「样本转写文本」 | 发送 `reference_text`（如果填写）；不带 `audio_options`，使用默认 `zero_shot` |
| **Cross-lingual** | 隐藏「样本转写文本」 | 发送空 `reference_text` 和 `audio_options: {"template_name": "cross_lingual"}` |
| **Instruct** | 显示「样本转写文本」+「Instruction 指令」 | 发送 `audio_options: {"template_name": "instruct"}`；填写指令时发送 `instructions` |
| **None** | 隐藏转写和指令 | 不发送 `reference_text`、`instructions` 或 `audio_options`，只保留 `voice` 参考 |

- Same language / Cross-lingual / Instruct 对应 CosyVoice3 的 `audio_options.template_name`；其它模型不支持时会忽略该选项
- None 是普通生成模式：仍然上传样本、创建/选择音色并发送 `voice`，但不带任何模板、转写或指令参数
- Cross-lingual 显式发送空 `reference_text`，避免服务端回退到保存音色里的转写
- 切换模式时，被隐藏字段的值会保留，切回来不会丢失

### 配置项

| 配置项 | 说明 |
|---|---|
| `api_key` | 服务端 API Key |
| `api_base` | 填到 `/v1` 为止，例如 `http://127.0.0.1:8888/v1` |
| `model` | 模型名；留空不发送，填写后原样传入（如 `audio-cpp/audio.cpp-gguf/CosyVoice3-GGUF`） |
| `mode` | 克隆模式：Same language / Cross-lingual / Instruct / None |
| `transcript` | 样本音频里说的原话；Cross-lingual 和 None 模式下隐藏且不发送 |
| `instructions` | Instruction 指令；仅 Instruct 模式显示并发送 |
| `language` | 生成文本语言，默认 `auto`（不发送提示） |
| `voice_id` | 下拉选择服务端已保存的音色；留空则用样本自动创建 |
| `voice_sample_name` | 指定使用哪个已上传样本；留空用第一个 |
| `format` | 输出格式，默认 `mp3` |
| `speed` | 语速；1.0 表示不发送该参数 |
| `seed` | 随机种子；留空不发送，填写整数则传入 |
| `timeout` | 超时秒数，默认 120 |

### 使用步骤

1. 在插件配置页的「音色克隆样本文件」上传 mp3/wav 样本（建议 10–30 秒干净人声）。
2. 在「TTS 引擎配置列表」新增 `[OpenAI] 兼容克隆`，填写 `api_key`、`api_base` 等配置。
3. 保存时会自动上传样本并创建保存音色，进度条显示「上传音色样本 → 创建保存音色」阶段。
4. 把该引擎设为第一个引擎，或在「当前生效的引擎实例名称」中填写其实例名。
5. 确认目标机器人已开启 AstrBot 的 TTS 功能，发送 `/tts_test 你好` 验证。

> **注意**
>
> - AstrBot 跑在 Docker 里时，`127.0.0.1` 指向容器自身，请把 `api_base` 改成 `http://host.docker.internal:8888/v1` 或宿主机局域网 IP。
> - 保存引擎时会预注册音色；服务端较慢时，保存对话框会显示上传/创建进度。WebUI 试听仍有 60 秒硬超时。
> - 服务端若重启后丢失保存音色，引擎检测到音色相关错误会自动重建一次。

### 与自定义 HTTP TTS 的区别

`[自定义] HTTP TTS` 只在一次请求里替换占位符，不会执行「上传样本 → 创建音色」两步；而 Unsloth `/v1/audio/speech` 只接受已保存音色的 id，不接受内联音频。OpenAI 兼容克隆引擎用于这种两步式接口。

---

## 歌曲/音乐生成（Unsloth `/v1/audio/run`）

调用本地 Unsloth Studio 音乐模型生成歌曲/配乐，并把结果挂到**当前被动回复**；群聊里不需要主动消息权限。

### 回复格式

用户直接描述需求；LLM 最终回复带上 `[[MUSIC]]` 标记即可：

```text
[[MUSIC]]
```

- 默认 `music_auto_route: true`：用户消息命中 BGM/歌曲需求，或回复带 `[[MUSIC]]` 标记，自动使用音乐引擎
- 普通对话继续走 TTS 引擎，不需要 `/tts_bind` 音乐引擎
- WebUI 引擎列表会给音乐引擎显示「音乐」徽标
- 插件把**用户消息**交给内置 chat 模型，生成英文 `style` 和 `lyrics`
- 然后调用 `/v1/audio/run`；过程中不需要用户或 skill 提供 JSON
- 没有歌词 → 器乐；有歌词 → 人声歌曲
- 生成成功后，标记会被移除，替换成一条 `Comp.Record` 语音
- 生成失败会回复“歌曲生成失败，请稍后重试”
- 音乐引擎绕过 TTS 触发概率、字数上限、智能跳过和用户意图跳过；只受插件总开关和 `/tts_off` 控制
- WebUI 试听写死 5 秒短样本和 style/lyrics，不调用 chat，只测试音乐模型

内置 chat 模型固定为：

```text
HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive:Q6_K
```

### 自动路由

| 配置项 | 说明 |
|---|---|
| `music_auto_route` | 开启后自动检测音乐请求；默认开启 |
| `music_route_patterns` | 匹配用户消息的正则列表；命中任意一条即使用音乐引擎 |

规则：

- 一次回复只判定一种模式：`music` / `tts` / `none`
- 命中音乐后**不会再触发 TTS**；普通对话只走 TTS
- 两个钩子各自独立判定模式，不依赖相对优先级
- 用户消息命中 `music_route_patterns`，或回复带 `[[MUSIC]]` → 使用**第一个音乐引擎**
- 其它回复 → 使用会话绑定/全局 TTS 引擎
- 如果会话绑定/全局是音乐引擎，普通对话会回退到第一个非音乐 TTS 引擎
- `require_marker` 只在关闭自动路由、手动绑定音乐引擎时生效

### 配置项

| 配置项 | 说明 |
|---|---|
| `api_key` | Studio API Key |
| `api_base` | Studio 地址，默认 `http://127.0.0.1:8888`；带不带 `/v1` 都可以，插件会自动归一化 |
| `model` | 音乐模型 ID；留空使用 Studio 当前加载的模型 |
| `duration` | 歌曲默认时长，默认 30 秒 |
| `require_marker` | 手动绑定模式下是否必须带 `[[MUSIC]]` 标记，默认开启；自动路由开启时由路由规则决定 |
| `timeout` | 音乐生成超时，默认 1800 秒 |

### 与 bgm-generation skill 的关系

bgm-generation 现在是一个轻量提示：识别 BGM/歌曲请求并回复 `[[MUSIC]]`。

模型选择、API Key、chat 模型、风格/歌词生成和音频发送全部由 universal_tts 插件完成；skill 不再查询模型、调用 API 或独立生成音频。

> 音乐生成可能需要几十秒到几分钟；回复会等待生成完成，超过平台回复窗口时可能被平台截断。

---

## 通用功能

- 会话级引擎绑定：不同群聊/私聊可使用不同引擎
- 自动内容路由：普通对话走 TTS，BGM/歌曲请求自动走音乐引擎
- 运行时热切换引擎，无需重启
- 全局 TTS 开关、单会话禁音、字数上限
- 智能跳过：代码块、纯链接、自定义正则命中的回复不朗读
- 用户意图跳过：介绍、解释、名称、定义、步骤、价格等知识型提问返回文本
- 引擎健康统计：成功/失败/耗时与最近错误
- WebUI 管理页：引擎增删改查/排序/试听、音色样本上传下载重命名、会话绑定、全局设置、配置导入导出、变更历史

### 文本 / 语音分流

插件有两层跳过规则：

| 配置项 | 匹配对象 | 用途 |
|---|---|---|
| `tts_skip_patterns` | 机器人回复文本 | 回复命中即不朗读（链接、JSON、工具输出等） |
| `tts_skip_user_intent_patterns` | **用户消息** | 用户问知识型问题时，整条回复返回文本 |

`tts_skip_user_intent_patterns` 默认覆盖：

```text
请问 / 介绍一下 / 解释一下 / 什么是 / 是什么 / 是谁 / 叫什么
名字 / 名称 / 全称 / 简称 / 别名 / 英文名 / 中文名
定义 / 含义 / 意思 / 列出 / 列举 / 对比 / 区别
步骤 / 教程 / 攻略 / 资料 / 来源 / 引用 / 查询 / 搜索
总结 / 概括 / 翻译 / 价格 / 多少钱 / 地址 / 电话 / 邮箱 / 网址 / 链接
怎么做 / 怎么用 / 怎么办 / 如何做 / 如何用 / 如何设置
在哪里 / 在哪儿 / 为什么 / 为啥
```

配置位置：插件 WebUI → **全局设置 → 用户意图跳过规则**，每行一个正则。也可以直接编辑 AstrBot 插件配置中的同名字段。

效果示例：

- 用户问「介绍一下 Python」→ 回复按文本发送
- 用户问「这个人的名字是什么」→ 回复按文本发送
- 用户说「今天怎么样」→ 正常触发语音

如果觉得范围太宽或太窄，增删对应的词即可；例如只想覆盖名称和定义，可以只保留：

```regex
(?:介绍|名称|名字|全称|简称|别名|定义|含义|意思)
```

### 指令

所有指令以 `/` 开头。管理命令默认仅管理员可用；无法识别管理员身份时，可在 `admin_user_ids` 中配置白名单。

| 指令 | 说明 |
|---|---|
| `/tts_help` | 查看指令帮助与当前状态 |
| `/tts_test [文本]` | 测试当前会话引擎的合成效果 |
| `/tts_status` | 查看 TTS 总开关、全局默认和当前会话状态 |
| `/tts_engines` | 列出所有已配置引擎 |
| `/tts_bind <序号>` | 为当前会话绑定专属引擎 |
| `/tts_bind 0` | 关闭当前会话 TTS |
| `/tts_unbind` | 解除当前会话绑定，回退全局 |
| `/tts_switch <序号>` | 切换全局默认引擎（管理员） |
| `/tts_switch 0` | 关闭全局默认 TTS（管理员） |
| `/tts_off` / `/tts_on` | 暂停 / 恢复全部会话的自动 TTS（管理员） |
| `/tts_bindings` | 查看全局引擎及所有会话绑定（管理员） |
| `/tts_unbind_all` | 清除所有会话绑定（管理员） |
| `/tts_limit_on` / `/tts_limit_off` | 开启 / 关闭 TTS 字数上限 |
| `/tts_limit_set <数字>` | 设置字数上限 |

---

## 安装

要求 AstrBot `>= 4.25.0`。

在 AstrBot 管理面板 → 插件市场 → 通过仓库地址安装，填入本仓库地址；或把本目录放入 `data/plugins/` 后重启。

---

## 开发与测试

```bash
pip install pyyaml httpx
python tests/test_webui.py      # 后端：Web API、引擎管理、绑定、导入导出、克隆引擎流程
python tests/test_frontend.py   # 前端：DOM / i18n / 标签一致性
```

测试在最小 stub 环境下加载真实插件代码，不依赖 AstrBot 运行时。

---

## 许可证

沿用原项目，基于 AGPL-3.0 协议开源。详见 [LICENSE](LICENSE)。
