"""Pipeline de voz: audio -> transcripción -> Claude -> tareas en segundo plano -> audio de respuesta."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .brain import Brain
from .browser_agent import ClinicAgent
from .config import Settings
from .dispatcher import CaregiverDispatcher
from .speech import SpeechQueue
from .stt import Transcriber
from .tasks import TaskManager
from .tts import Synthesizer

log = logging.getLogger("pipeline")

MIN_TRANSCRIPT_CHARS = 2
NOT_HEARD = "Disculpe, no la alcancé a escuchar bien. ¿Me lo puede repetir, por favor?"
TROUBLE = "Disculpe, tuve un problema para escucharla. ¿Lo intentamos de nuevo?"


@dataclass
class Services:
    settings: Settings
    transcriber: Transcriber
    brain: Brain
    synthesizer: Synthesizer
    dispatcher: CaregiverDispatcher
    clinic_agent: ClinicAgent
    tasks: TaskManager
    speech: SpeechQueue | None = None

    @classmethod
    def build(cls, settings: Settings) -> "Services":
        dispatcher = CaregiverDispatcher(settings)
        clinic_agent = ClinicAgent(settings)
        synthesizer = Synthesizer(settings)
        return cls(
            settings=settings,
            transcriber=Transcriber(settings),
            brain=Brain(settings),
            synthesizer=synthesizer,
            dispatcher=dispatcher,
            clinic_agent=clinic_agent,
            tasks=TaskManager(settings, dispatcher, clinic_agent),
            speech=SpeechQueue(synthesizer),
        )


@dataclass
class PipelineResult:
    audio: bytes
    transcript: str
    reply: str
    task_ids: list[str]
    error: str | None = None


class VoicePipeline:
    def __init__(self, services: Services):
        self.s = services
        if services.speech is None:
            services.speech = SpeechQueue(services.synthesizer)
        services.tasks.announcer = self.announce  # los resultados de las tareas vuelven a la persona como voz

    async def announce(self, notice: str) -> None:
        """Una tarea terminó: Claude convierte el resultado en lo que se le dirá a la persona y queda en la cola de voz."""
        s = self.s
        try:
            result = await s.brain.follow_up(notice, s.tasks.context())
            reply = result.reply
            # Un aviso automático no puede lanzar trámites por su cuenta; solo avisar a la cuidadora.
            alerts = [c for c in result.tool_calls if c.name == "avisar_al_cuidador"]
            s.tasks.dispatch(alerts, "", reply)
        except Exception as exc:
            log.exception("Falló el aviso hablado")
            s.tasks.report_system_error("El asistente no pudo comunicarle un resultado a la persona.", details={"stage": "announce", "error": type(exc).__name__})
            reply = "Disculpe, tuve un problema con lo que estaba haciendo. ¿Lo intentamos de nuevo?"
        await s.speech.push(reply)

    async def handle(self, audio: bytes) -> PipelineResult:
        s = self.s

        async def speak(text: str, transcript: str = "", task_ids: list[str] | None = None, error: str | None = None) -> PipelineResult:
            return PipelineResult(await s.synthesizer.synthesize(text), transcript, text, task_ids or [], error)

        t0 = time.perf_counter()

        # 1. Voz -> texto
        try:
            transcript = await s.transcriber.transcribe(audio)
        except Exception as exc:
            log.exception("Falló la transcripción")
            s.tasks.report_system_error("No se pudo transcribir el audio del adulto mayor.", details={"stage": "stt", "error": type(exc).__name__})
            return await speak(TROUBLE, error="stt")
        t1 = time.perf_counter()
        log.info("Transcripción: %s", transcript)
        if len(transcript) < MIN_TRANSCRIPT_CHARS:
            return await speak(NOT_HEARD, transcript, error="empty_transcript")

        # 2. Razonamiento con herramientas
        try:
            result = await s.brain.think(transcript, s.tasks.context())
        except Exception as exc:
            log.exception("Falló el razonamiento")
            s.tasks.report_system_error("El asistente no pudo procesar lo que dijo el adulto mayor.", transcript, {"stage": "llm", "error": type(exc).__name__})
            return await speak(TROUBLE, transcript, error="llm")
        if result.refused:
            s.tasks.report_system_error("El asistente no pudo ayudar con una solicitud del adulto mayor.", transcript, {"stage": "llm", "error": "refusal"})

        t2 = time.perf_counter()

        # 3. El trabajo parte en segundo plano mientras se genera la voz (una salvaguarda puede cambiar la frase)
        task_ids, reply = s.tasks.dispatch(result.tool_calls, transcript, result.reply)

        # 4. Texto -> voz
        spoken = await speak(reply, transcript, task_ids)
        t3 = time.perf_counter()
        log.info("Tiempos: stt=%.1fs claude=%.1fs tts=%.1fs total=%.1fs", t1 - t0, t2 - t1, t3 - t2, t3 - t0)
        return spoken
