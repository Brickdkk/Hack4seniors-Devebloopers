"""Backend FastAPI. Arranque: uvicorn app.main:app --port 8000"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response

from .config import Settings
from .pipeline import Services, VoicePipeline
from .schemas import TaskInfo
from .tts import TTSError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "groq", "asyncio", "edge_tts"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("api")

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # límite de Groq
HEADER_TEXT_MAX = 600


async def _read_audio(request: Request) -> bytes:
    """El audio llega como cuerpo crudo de la petición (WAV)."""
    data = await request.body()
    if not data:
        raise HTTPException(422, "El audio está vacío.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "El audio supera los 25 MB.")
    return data


def _h(text: str) -> str:
    """Los headers HTTP son ASCII: el texto va codificado en porcentaje (UTF-8)."""
    return quote(text[:HEADER_TEXT_MAX], safe="")


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or Services.build(Settings.from_env())
        app.state.services = svc
        app.state.pipeline = VoicePipeline(svc)
        yield
        await svc.tasks.aclose()
        await svc.dispatcher.aclose()

    app = FastAPI(title="Hack4Seniors - núcleo de voz", lifespan=lifespan)

    @app.get("/health")
    async def health():
        s: Settings = app.state.services.settings
        return {
            "status": "ok",
            "model": s.claude_model,
            "keys": {"ANTHROPIC_API_KEY": bool(os.getenv("ANTHROPIC_API_KEY")), "GROQ_API_KEY": bool(os.getenv("GROQ_API_KEY"))},
            "clinic_portal_url": s.clinic_portal_url,
            "caregiver_panel_url": s.caregiver_panel_url,
        }

    @app.post("/api/process-audio")
    async def process_audio(request: Request):
        """Recibe un WAV (cuerpo crudo) y responde con el audio de la voz (WAV). Transcripción, texto de respuesta y
        ids de tareas viajan en los headers X-Transcript, X-Reply y X-Task-Ids (texto en %-encoding UTF-8)."""
        audio = await _read_audio(request)
        try:
            result = await app.state.pipeline.handle(audio)
        except TTSError as exc:
            raise HTTPException(502, f"No se pudo generar la voz: {exc}")
        except Exception as exc:
            log.exception("Falló la síntesis de voz")
            raise HTTPException(502, f"No se pudo generar la voz: {type(exc).__name__}")
        headers = {
            "X-Transcript": _h(result.transcript),
            "X-Reply": _h(result.reply),
            "X-Task-Ids": ",".join(result.task_ids),
        }
        if result.error:
            headers["X-Error"] = result.error
        return Response(content=result.audio, media_type="audio/wav", headers=headers)

    @app.get("/api/speech/next")
    async def next_speech():
        """Avisos hablados del sistema (p. ej. 'encontré estas horas...'). mic_client.py lo consulta cada un par de segundos:
        200 con el audio WAV (texto en X-Reply) o 204 si no hay nada pendiente."""
        try:
            got = await app.state.services.speech.pop()
        except Exception as exc:
            log.exception("Falló la síntesis del aviso")
            raise HTTPException(502, f"No se pudo generar la voz: {type(exc).__name__}")
        if got is None:
            return Response(status_code=204)
        item, audio = got
        return Response(content=audio, media_type="audio/wav", headers={"X-Reply": _h(item.text), "X-Speech-Id": item.id})

    @app.get("/api/tasks", response_model=list[TaskInfo])
    async def list_tasks():
        return app.state.services.tasks.list()

    @app.get("/api/tasks/{task_id}", response_model=TaskInfo)
    async def get_task(task_id: str):
        task = app.state.services.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Tarea no encontrada.")
        return task

    return app


app = create_app()
