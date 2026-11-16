# Universal TTS（个人分支）

本仓库是 [CyrilPeng/astrbot_plugin_universal_tts](https://github.com/CyrilPeng/astrbot_plugin_universal_tts) 的分支，保留原项目的全部引擎与管理功能，并额外增加了 **OpenAI 兼容音色克隆** 支持。

> 原项目：<https://github.com/CyrilPeng/astrbot_plugin_universal_tts>
>
> 本分支的仓库地址请以实际 fork 为准。安装时在 AstrBot 插件市场选择「通过仓库地址安装」，填入本分支地址。

---

## 本分支新增

### `[OpenAI] 兼容克隆（上传样本 → 保存音色 → 合成）`

用于接入在标准 OpenAI 语音接口之外、额外提供「上传音频输入」和「保存音色」路由的服务，例如 **Unsloth Studio / audio.cpp（CosyVoice3 GGUF）**：

```text
POST {api_base}/audio/inputs?name=<文件名>   原始音频字节  -> {"id": <input_id>}
POST {api_base}/audio/voices                 {"name", "source": {"input_id"}} -> {"id": <voice_id>}
POST {api_base}/audio/speech                 {"model", "voice", "input", "response_format"} -> 音频二进制
```

特性：

- **首次合成自动注册音色**：读取插件里上传的样本，调用 `inputs` 上传、`voices` 创建保存音色，再调用 `speech` 合成；之后直接复用 `voice_id`
- **音色缓存落盘**：缓存写在 `data/plugin_data/astrbot_plugin_universal_tts/openai_clone_voices.json`，重启后优先复用；样本内容或服务地址变化时自动重新创建
- **音色名默认取样本文件名**：例如 `me.wav` 创建的保存音色就叫 `me`，无需单独填写名称
- **失效自愈**：服务端重启或删除音色后，合成返回音色相关错误时会自动重新上传样本并重建一次
- **可跳过自动注册**：已有保存音色时，下拉选择 `voice_id` 即可，不再上传样本
- **可查看服务端音色**：`voice_id` 是下拉框，打开编辑器时自动调用 `GET /audio/voices` 加载服务端已保存的音色并按名称展示；留空表示自动创建，旁边的「读取已有音色」可手动刷新
- **生成语言可选**：`language` 默认 `auto`，不发送语言提示；选择 `Chinese`、`Japanese` 等后会作为 `language` 参数发送给支持该提示的模型
- **可固定随机种子**：`seed` 留空时不发送；填写整数会作为 `seed` 参数传入（best-effort 确定性，不保证完全复现）
- **模型名可选**：`model` 留空时请求体不发送该字段，由服务端使用当前加载的模型；填写后原样传入你填写的模型名

### 使用步骤

1. 在插件配置页的「音色克隆样本文件」上传 mp3/wav 样本（建议 10–30 秒干净人声）。
2. 在「TTS 引擎配置列表」新增 `[OpenAI] 兼容克隆`，填写：

   | 配置项 | 说明 |
   |---|---|
   | `api_key` | 服务端 API Key |
   | `api_base` | 填到 `/v1` 为止，例如 `http://127.0.0.1:8888/v1` |
   | `model` | 模型名；留空不发送，填写后原样传入（如 `audio-cpp/audio.cpp-gguf/CosyVoice3-GGUF`） |
   | `transcript` | 样本音频里说的原话；部分模型克隆时需要，可留空 |
   | `language` | 生成文本语言，默认 `auto`（不发送提示）；可选 Chinese / English / Japanese 等 |
   | `voice_id` | 下拉选择服务端已保存的音色；留空则用样本自动创建 |
   | `voice_sample_name` | 指定使用哪个已上传样本；留空用第一个 |
   | `format` | 输出格式，默认 `mp3` |
   | `speed` | 语速；1.0 表示不发送该参数 |
   | `seed` | 随机种子；留空不发送，填写整数则传入 |
   | `timeout` | 超时秒数，默认 120 |

3. 把该引擎设为第一个引擎，或在「当前生效的引擎实例名称」中填写其实例名。
4. 确认目标机器人已开启 AstrBot 的 TTS 功能，然后发送 `/tts_test 你好` 验证。

> **注意**
>
> - AstrBot 跑在 Docker 里时，`127.0.0.1` 指向容器自身，请把 `api_base` 改成 `http://host.docker.internal:8888/v1` 或宿主机局域网 IP。
> - 首次合成需要上传样本并创建音色，可能比后续慢；WebUI 试听有 60 秒硬超时，首次验证建议用 `/tts_test`。
> - 服务端若重启后丢失保存音色，引擎检测到音色相关错误会自动重建一次，无需手动处理。

### 与自定义 HTTP 的区别

`[自定义] HTTP TTS` 只在**一次请求**里替换占位符，不会执行「上传样本 → 创建音色」两步；而 Unsloth `/v1/audio/speech` 只接受已保存音色的 id，不接受内联音频。本分支新增的引擎正是为这种两步式接口准备的。

---

## 保留的原项目功能

- 多引擎：MiMo V2.5 / VoiceDesign / VoiceClone、OpenAI 兼容、火山引擎、阿里云、Azure、ElevenLabs、自定义 HTTP
- 会话级引擎绑定、运行时热切换、全局开关与字数上限
- WebUI 管理页：引擎增删改查/排序/试听、样本上传下载重命名、会话绑定、全局设置、配置导入导出
- 智能跳过（代码块、纯链接、自定义正则）与引擎健康统计
- 所有指令以 `/` 开头；`/tts_help` 查看完整帮助

---

## 安装

要求 AstrBot `>= 4.25.0`。

在 AstrBot 管理面板 → 插件市场 → 通过仓库地址安装，填入本分支仓库地址；或把本目录放入 `data/plugins/` 后重启。

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
