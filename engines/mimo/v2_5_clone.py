"""MiMo V2.5 TTS VoiceClone 引擎"""

import base64

import httpx

from ..base import TTSEngine


class MiMoV25CloneEngine(TTSEngine):
    """小米 MiMo-V2.5-TTS-VoiceClone 引擎（音频样本克隆音色）"""

    def __init__(self, config: dict, plugin_config: dict | None = None) -> None:
        super().__init__(config, plugin_config)
        self.api_key: str = config.get("api_key", "")
        self.api_base: str = config.get("api_base", "https://api.xiaomimimo.com/v1")
        self.model: str = config.get("model", "mimo-v2.5-tts-voiceclone")
        self.audio_format: str = config.get("format", "wav")
        self.style_prompt: str = config.get("style_prompt", "")
        self.timeout: int = config.get("timeout", 60)
        self._client: httpx.AsyncClient | None = None
        self._voice_base64: str | None = None
        self._sample_paths: list[str] = self.plugin_config.get("voice_clone_samples", [])
        self._sample_name: str = config.get("voice_sample_name", "")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _get_voice_base64(self) -> str:
        if self._voice_base64 is None:
            path = self._resolve_sample_path(self._sample_paths, self._sample_name)
            self._voice_base64 = self._encode_sample_base64(path, with_prefix=True)
        return self._voice_base64

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        client = self._get_client()
        url = f"{self.api_base.rstrip('/')}/chat/completions"

        messages = []
        messages.append({"role": "user", "content": self.style_prompt.strip()})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": self.audio_format, "voice": self._get_voice_base64()},
        }

        resp = await client.post(
            url, headers={"api-key": self.api_key, "Content-Type": "application/json"}, json=payload
        )
        resp.raise_for_status()

        data = resp.json()
        audio_data = data["choices"][0]["message"]["audio"]["data"]
        return base64.b64decode(audio_data), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
