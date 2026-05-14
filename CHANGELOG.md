# 更新日志

## v1.0.1

- 优化音色克隆引擎：将音色样本配置从手动填写路径改为 WebUI 文件上传，用户无需关心服务器文件路径
- 支持 AstrBot v4.13.0+ 的 `file` 类型配置项

## v1.0.0

- 初始版本
- 支持 MiMo-V2-TTS（预置音色 + style 标签风格控制）
- 支持 MiMo-V2.5-TTS（精品预置音色 + 自然语言风格控制）
- 支持 MiMo-V2.5-TTS-VoiceDesign（文本描述设计音色）
- 支持 MiMo-V2.5-TTS-VoiceClone（音频样本克隆音色）
- 支持 OpenAI 兼容 TTS 接口
- 插件自动注册为 AstrBot TTS Provider，走官方 TTS 管道
- 支持运行时通过指令切换引擎
