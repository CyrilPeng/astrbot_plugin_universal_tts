# 更新日志

## v1.2.0

### 新增
- **会话级引擎绑定**：不同群聊/私聊可配置不同的 TTS 引擎，互不干扰
- `/tts_bind <序号或实例名>`：为当前会话绑定指定引擎
- `/tts_unbind`：解除当前会话的绑定，回退到全局默认
- 绑定关系持久化存储，重启不丢失
- `/tts_engines` 和 `/tts_switch` 显示中标注"全局"和"本会话"状态

### 改进
- `/tts_test` 现在使用当前会话绑定的引擎（而非固定全局引擎）
- `/tts_switch` 切换全局引擎后同步更新配置中的 `active_engine` 字段
- `/tts_switch` 作为管理员命令，不在公开提示中暴露
- 修复 `event.message_str` 包含命令名导致参数解析错误的问题
- 修复 `result_message` 方法不存在的问题，改用 `chain_result`

## v1.1.1

### 改进
- 指令规范化：所有指令必须以 `/` 开头才会响应，避免误触发
- `/tts_engines` 改为显示已配置的引擎实例列表（序号 + 实例名）
- `/tts_switch` 支持按序号快速切换，如 `/tts_switch 2`
- VoiceDesign 和 VoiceClone 引擎的模型名称改为可配置项，支持重置为默认值

## v1.1.0

### 新增引擎
- **火山引擎 TTS**：基于 SAMI HTTP API，支持多种发音人和语速/音量调节
- **阿里云百炼 CosyVoice TTS**：基于 DashScope OpenAI 兼容接口
- **Azure Cognitive Services TTS**：支持 SSML、情感风格和角色扮演
- **ElevenLabs TTS**：高质量多语言语音合成
- **自定义 HTTP TTS**：模板化配置接入任意 HTTP TTS API，支持占位符替换和多种响应解析

### 架构改造
- 引擎代码按提供商重组为多级目录结构（mimo/、openai/、volcengine/、aliyun/、azure/、elevenlabs/、custom_http/）
- 配置模板按 `[提供商] 模型` 格式分组，用户选择时一目了然
- 提取公共路径解析和 base64 编码逻辑到基类，精简各引擎代码

### 改进
- 错误日志包含引擎名称和异常类型，便于定位问题
- 配置字段精简，去除冗余描述

## v1.0.1

- 优化音色克隆引擎：将音色样本配置从手动填写路径改为 WebUI 文件上传
- 支持 AstrBot v4.13.0+ 的 `file` 类型配置项

## v1.0.0

- 初始版本
- 支持 MiMo-V2-TTS、MiMo-V2.5-TTS、VoiceDesign、VoiceClone
- 支持 OpenAI 兼容 TTS 接口
- 插件自动注册为 AstrBot TTS Provider
- 支持运行时通过指令切换引擎
