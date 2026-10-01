"""Modelos de datos compartidos: el evento que recibe el Panel del Cuidador y el estado de las tareas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

EventType = Literal["clinic_task", "caregiver_alert", "emergency", "system_error"]
EventStatus = Literal["success", "error", "info"]
Severity = Literal["info", "important", "emergency"]
TaskStatus = Literal["queued", "running", "success", "error"]


def now_iso(tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = None
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    return now.isoformat(timespec="seconds")


class CaregiverEvent(BaseModel):
    """Payload JSON que se envía por HTTP POST a CAREGIVER_PANEL_URL."""

    event_id: str
    event_type: EventType
    status: EventStatus
    severity: Severity = "info"
    timestamp: str = Field(description="ISO 8601 con zona horaria")
    elder_name: str
    summary: str = Field(description="Texto corto en español, listo para mostrar al cuidador")
    user_request: str = Field(default="", description="Lo que dijo el adulto mayor (transcripción)")
    details: dict[str, Any] = Field(default_factory=dict)
    # Datos exactos que no deben salir hacia el panel (nombre, RUT y teléfono dados por la persona). No se serializa.
    redact: list[str] = Field(default_factory=list, exclude=True)


class TaskInfo(BaseModel):
    id: str
    kind: str
    status: TaskStatus = "queued"
    summary: str = ""
    created_at: str
    finished_at: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
