"""Adaptador al Panel del Cuidador desplegado: traduce nuestros eventos a su esquema de logs (POST {panel}/api/logs).

El panel (static/datalogger.js) espera:
  {source, actor, elder, category, action, status, title, message, due_at, create_reminder, metadata}
con category en cita_medica|tramite|mensaje|recordatorio|medicacion|salud|alerta|digest|navegacion|general,
y status en ok|info|alerta (alerta y error se muestran como alerta). Se usan solo los valores de `action` que el
panel ya conoce. El panel es público y sin autenticación: aquí no se envían RUT, teléfono ni rutas locales.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import bare_name
from .schemas import CaregiverEvent

SOURCE = "agente_llm"
ACTOR = "Asistente de voz"
ACTION_WEB = "ejecutar_tramite_web"
ACTION_MESSAGE = "enviar_mensaje_familiar"
ACTION_ALERT = "reporte_diario_y_alerta_sintomas"
TRAMITE_LABELS = {"cancelar_hora": "cancelar una hora", "consultar_horas": "consultar horas", "agendar_hora": "agendar una hora"}


# Datos personales que nunca deben salir hacia el panel (es público): RUT y teléfonos chilenos.
RUT_RE = re.compile(r"\b\d{1,2}(?:\.\d{3}){2}-[\dkK]\b|\b\d{7,8}-[\dkK]\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?56[\s-]?)?9[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
CODE_RE = re.compile(r"\b([A-Z]{2,6}-?\d{3,})\b")  # CONF-4821, RES123456
HIDDEN = "[dato oculto]"


def scrub(value: Any, exact: tuple[re.Pattern, ...] = ()) -> Any:
    """Oculta RUT, teléfonos y los datos exactos indicados en cualquier texto (también dentro de diccionarios y listas)."""
    if isinstance(value, str):
        for pattern in exact:
            value = pattern.sub(HIDDEN, value)
        return PHONE_RE.sub(HIDDEN, RUT_RE.sub(HIDDEN, value))
    if isinstance(value, dict):
        return {k: scrub(v, exact) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, exact) for v in value]
    return value


def confirmation_code(text: str) -> str | None:
    """Solo el código de la confirmación: la pantalla del portal suele repetir además los datos de la paciente."""
    m = CODE_RE.search(text or "")
    return m.group(1) if m else None


def panel_endpoint(url: str) -> str:
    """Si la URL es solo el sitio (https://panel.vercel.app/), los logs van a /api/logs; si trae ruta, se respeta."""
    parsed = urlparse(url.strip())
    if parsed.path in ("", "/"):
        return url.strip().rstrip("/") + "/api/logs"
    return url.strip()


def panel_elder_name(name: str) -> str:
    """El panel identifica al adulto mayor por nombre, sin 'don/doña'."""
    return bare_name(name)


def appointment_due_at(appointment: dict[str, Any], tz: str) -> str | None:
    """ISO 8601 con zona horaria de la cita, o None si el portal no entregó fecha y hora válidas."""
    try:
        naive = datetime.fromisoformat(f"{appointment.get('date', '')}T{appointment.get('time', '')}")
    except ValueError:
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:
        return naive.astimezone().isoformat(timespec="seconds")  # hora local del sistema
    return naive.replace(tzinfo=zone).isoformat(timespec="seconds")


def _meta(event: CaregiverEvent, **extra: Any) -> dict[str, Any]:
    meta = {"event_id": event.event_id, "event_type": event.event_type, "severity": event.severity, **extra}
    return {k: v for k, v in meta.items() if v not in ("", None)}


def to_panel_log(event: CaregiverEvent, caregiver_name: str, tz: str) -> dict[str, Any]:
    """Esquema del panel, con RUT, teléfonos y los datos que dio la persona ocultos en todos los textos."""
    log = _to_panel_log(event, caregiver_name, tz)
    elder = log["elder"]
    exact = tuple(re.compile(re.escape(v), re.IGNORECASE) for v in sorted(set(event.redact), key=len, reverse=True) if len(v.strip()) >= 4)
    clean = scrub(log, exact)
    clean["elder"] = elder  # el panel asocia el evento por este nombre: no se toca
    return clean


def _to_panel_log(event: CaregiverEvent, caregiver_name: str, tz: str) -> dict[str, Any]:
    d = event.details
    log: dict[str, Any] = {"source": SOURCE, "actor": ACTOR, "elder": panel_elder_name(event.elder_name)}

    if event.event_type == "clinic_task":
        task = d.get("task", "")
        specialty = (d.get("specialty") or "").strip().capitalize()
        if task == "agendar_hora" and event.status == "success":
            appt = d.get("appointment", {})
            due = appointment_due_at(appt, tz)
            code = confirmation_code(d.get("confirmation", ""))
            return {
                **log, "category": "cita_medica", "action": ACTION_WEB, "status": "ok",
                "title": f"Hora médica de {specialty}" if specialty else "Hora médica",
                "message": event.summary, "due_at": due, "create_reminder": due is not None,
                "metadata": _meta(event, especialidad=specialty, profesional=appt.get("professional"), fecha=appt.get("date"), hora=appt.get("time"), codigo=code),
            }
        label = TRAMITE_LABELS.get(task, "hacer un trámite")
        stage = d.get("stage")
        if event.status == "success":
            title = f"Trámite: {label}"
        elif stage == "search":
            title = "No se pudieron buscar horas"
        else:
            title = f"No se pudo {label}"
        return {
            **log, "category": "tramite", "action": ACTION_WEB, "status": "ok" if event.status == "success" else "alerta",
            "title": title, "message": event.summary, "metadata": _meta(event, tramite=task, especialidad=specialty, stage=stage),
        }

    if event.event_type == "caregiver_alert" and event.severity == "info":
        return {
            **log, "category": "mensaje", "action": ACTION_MESSAGE, "status": "ok",
            "title": f"Recado para {caregiver_name}", "message": f"«{event.summary}»",
            "metadata": _meta(event, destinatario=caregiver_name, urgencia=d.get("urgency")),
        }

    if event.event_type in ("caregiver_alert", "emergency"):
        emergency = event.event_type == "emergency"
        return {
            **log, "category": "alerta", "action": ACTION_ALERT, "status": "alerta",
            "title": f"🚨 Emergencia de {log['elder']}" if emergency else f"Aviso importante de {log['elder']}",
            "message": event.summary, "metadata": _meta(event, urgencia=d.get("urgency")),
        }

    # system_error
    return {
        **log, "category": "alerta", "action": ACTION_ALERT, "status": "alerta",
        "title": "Falla técnica del asistente de voz", "message": event.summary,
        "metadata": _meta(event, stage=d.get("stage"), error=d.get("error")),
    }
