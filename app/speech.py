"""Cola de avisos hablados: lo que el sistema le dice a la persona cuando termina una tarea en segundo plano.

mic_client.py consulta GET /api/speech/next cada un par de segundos y reproduce lo que haya por el parlante de la laptop.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from .tts import Synthesizer

log = logging.getLogger("speech")

MAX_AGE_S = 900  # un aviso de hace más de 15 minutos ya no sirve


@dataclass
class SpeechItem:
    text: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.monotonic)
    audio: bytes | None = None


class SpeechQueue:
    def __init__(self, synthesizer: Synthesizer):
        self.synthesizer = synthesizer
        self._items: deque[SpeechItem] = deque()

    def __len__(self) -> int:
        return len(self._items)

    async def push(self, text: str) -> SpeechItem:
        item = SpeechItem(text)
        try:
            item.audio = await self.synthesizer.synthesize(text)  # listo de antemano: al entregarlo no hay espera
        except Exception:
            log.exception("No se pudo pre-sintetizar el aviso; se intentará al entregarlo")
        self._items.append(item)
        return item

    async def pop(self) -> tuple[SpeechItem, bytes] | None:
        while self._items and time.monotonic() - self._items[0].created > MAX_AGE_S:
            self._items.popleft()
        if not self._items:
            return None
        item = self._items.popleft()
        try:
            if item.audio is None:
                item.audio = await self.synthesizer.synthesize(item.text)
        except Exception:
            self._items.appendleft(item)  # no se pierde: se reintenta en la próxima consulta
            raise
        return item, item.audio
