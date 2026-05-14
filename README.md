# astrbot_plugin_universal_tts

通用 TTS 插件 for [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

支持多种 TTS 引擎，直接注册为 AstrBot TTS Provider，完全走官方 TTS 管道（触发概率、双输出等设置均生效）。

## 特性

- **多引擎支持**：MiMo V2/V2.5、OpenAI 兼容、火山引擎、阿里云百炼、Azure、ElevenLabs、自定义 HTTP
- **无缝集成**：注册为 AstrBot TTS Provider，无需修改 AstrBot 配置
- **热切换**：通过指令在运行时切换不同引擎
- **WebUI 配置**：所有参数均可在 AstrBot 管理面板中可视化配置
- **自定义 HTTP**：通过模板化配置接入任意 HTTP TTS API，无需编写代码

## 安装

在 AstrBot 管理面板中通过仓库地址安装：

```
https://github.com/CyrilPeng/astrbot_plugin_universal_tts
```

## 配置

安装后在插件配置页面：

1. 在「TTS 引擎配置列表」中添加引擎实例
2. 填写对应的 API Key、音色等参数
3. 确保 AstrBot 设置中 TTS 功能已开启

插件会自动将自身设为默认 TTS Provider。

## 支持的引擎

| 引擎类型 | 说明 |
|---------|------|
| MiMo-V2-TTS | 小米 MiMo V2，支持预置音色和 `<style>` 标签风格控制 |
| MiMo-V2.5-TTS | 小米 MiMo V2.5，支持精品音色和自然语言风格控制 |
| MiMo-V2.5-VoiceDesign | 通过文本描述设计音色，无需音频样本 |
| MiMo-V2.5-VoiceClone | 基于音频样本复刻任意音色 |
| OpenAI 兼容 TTS | 兼容 OpenAI `/v1/audio/speech` 接口的服务 |
| 火山引擎 TTS | 字节跳动火山引擎 SAMI HTTP API |
| 阿里云百炼 TTS | 阿里云百炼 CosyVoice，DashScope 兼容接口 |
| Azure TTS | 微软 Azure Cognitive Services 语音服务 |
| ElevenLabs TTS | ElevenLabs 高质量多语言语音合成 |
| 自定义 HTTP TTS | 模板化配置接入任意 HTTP TTS API |

## 自定义 HTTP 引擎

自定义 HTTP 引擎允许你通过模板化配置接入任何 HTTP TTS API，无需编写代码。

### 占位符

| 占位符 | 说明 |
|--------|------|
| `${TEXT}` | 待合成的文本（LLM 回复内容） |
| `${API_KEY}` | 用户配置的 API 密钥 |
| `${VOICE_SAMPLE_BASE64}` | 音色样本 base64 编码（含 data URI 前缀） |
| `${VOICE_SAMPLE_BASE64_RAW}` | 音色样本纯 base64 编码 |
| `${VOICE_SAMPLE_PATH}` | 音色样本文件路径 |

### 配置示例

**接入返回二进制音频的 API：**
```
URL: https://api.example.com/v1/tts
Method: POST
Headers: Authorization: Bearer ${API_KEY}
Body: {"text": "${TEXT}", "voice": "alloy"}
Response Type: binary
Audio Format: mp3
```

**接入返回 JSON 中 base64 音频的 API：**
```
URL: https://api.example.com/v1/chat/completions
Method: POST
Headers: api-key: ${API_KEY}
Body: {"model": "tts-model", "messages": [{"role": "assistant", "content": "${TEXT}"}]}
Response Type: json
Response Audio Path: choices.0.message.audio.data
Response Audio Encoding: base64
Audio Format: wav
```

## 指令

| 指令 | 说明 |
|------|------|
| `/tts_test [文本]` | 测试 TTS 合成 |
| `/tts_switch` | 查看已配置的引擎列表 |
| `/tts_switch <实例名>` | 切换到指定引擎 |
| `/tts_engines` | 列出所有支持的引擎类型 |

## 工作原理

插件在加载时：
1. 根据配置创建 TTS 引擎实例
2. 动态创建继承 `TTSProvider` 的 Provider 类
3. 将 Provider 实例注入 AstrBot 的 `ProviderManager`
4. 设为默认 TTS Provider

之后所有 TTS 触发逻辑（概率、会话控制、dual_output 等）完全走 AstrBot 官方管道，只是底层合成由本插件的引擎完成。

## 扩展新引擎

1. 在 `engines/` 目录下新建提供商子目录（如 `engines/newprovider/`）
2. 创建引擎类，继承 `TTSEngine`，实现 `synthesize(text) -> (bytes, format)` 方法
3. 在子目录的 `__init__.py` 中导出引擎类
4. 在 `engines/__init__.py` 中导入并注册到 `ENGINE_REGISTRY`
5. 在 `_conf_schema.json` 的 `templates` 中添加对应配置模板

## 依赖

- httpx >= 0.27.0

## 许可

MIT
