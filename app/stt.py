"""Speech-to-Text con Groq Whisper."""

from __future__ import annotations

from groq import AsyncGroq

from .config import Settings


class Transcriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: AsyncGroq | None = None

    @property
    def client(self) -> AsyncGroq:
        # Perezoso: AsyncGroq() falla al construirse si falta GROQ_API_KEY.
        if self._client is None:
            self._client = AsyncGroq(timeout=20.0, max_retries=1)
        return self._client

    async def transcribe(self, audio: bytes, filename: str = "audio.wav") -> str:
        result = await self.client.audio.transcriptions.create(
            file=(filename, audio),
            model=self.settings.groq_stt_model,
            language=self.settings.stt_language,
            response_format="json",
            temperature=0.0,
        )
        return (result.text or "").strip()
