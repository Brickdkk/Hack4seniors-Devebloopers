"""Configuración leída desde variables de entorno (.env)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _str(name: str, default: str) -> str:
    raw = os.getenv(name, "").strip()
    return raw or default


def bare_name(name: str) -> str:
    """'don Eliecer' / 'doña Rosa' -> 'Eliecer' / 'Rosa': el asistente llama a la persona solo por su nombre."""
    return re.sub(r"^(?:don|doña)\s+", "", name.strip(), flags=re.IGNORECASE)


def format_rut(raw: str) -> str:
    """'123456785', '12345678-5' o '12.345.678-5' -> '12.345.678-5'. Si no parece un RUT, se devuelve tal cual."""
    compact = re.sub(r"[.\s-]", "", raw).upper()
    if not re.fullmatch(r"\d{7,8}[\dK]", compact):
        return raw.strip()
    return f"{int(compact[:-1]):,}".replace(",", ".") + "-" + compact[-1]


def _resolve_headless(raw: str) -> bool:
    """false = ventana visible, true = headless, auto = visible solo si hay pantalla."""
    raw = raw.strip().lower()
    if raw == "auto":
        if sys.platform in ("win32", "darwin"):
            return False
        return not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Orquestador (Claude)
    claude_model: str
    claude_effort: str
    agent_effort: str
    refusal_fallback: bool
    # STT / TTS
    groq_stt_model: str
    stt_language: str
    tts_voice: str
    tts_rate: str
    # Integraciones de los compañeros
    clinic_portal_url: str
    caregiver_panel_url: str
    # Personas (datos guardados de la persona mayor)
    elder_name: str
    caregiver_name: str
    patient_full_name: str
    patient_rut: str
    patient_phone: str
    patient_age: str
    emergency_number: str
    # Agente de navegación
    browser_headless: bool
    browser_slow_mo_ms: int
    browser_keep_open_s: int
    agent_max_steps: int
    agent_timeout_s: int
    # Varios
    timezone: str
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        return cls(
            claude_model=_str("LLM_MODEL", "claude-sonnet-5-5"),
            claude_effort=_str("LLM_EFFORT", "low"),
            agent_effort=_str("AGENT_EFFORT", "medium"),
            refusal_fallback=_bool("LLM_REFUSAL_FALLBACK", True),
            groq_stt_model=_str("GROQ_STT_MODEL", "whisper-large-v3"),
            stt_language=_str("STT_LANGUAGE", "es"),
            tts_voice=_str("TTS_VOICE", "es-CL-CatalinaNeural"),
            tts_rate=_str("TTS_RATE", "+0%"),
            clinic_portal_url=_str("CLINIC_PORTAL_URL", "http://localhost:8080"),
            caregiver_panel_url=_str("CAREGIVER_PANEL_URL", "http://localhost:3000/api/events"),
            elder_name=_str("ELDER_NAME", "Eliecer"),
            caregiver_name=_str("CAREGIVER_NAME", "Camila"),
            patient_full_name=_str("PATIENT_FULL_NAME", ""),
            patient_rut=format_rut(_str("PATIENT_RUT", "")),
            patient_phone=_str("PATIENT_PHONE", ""),
            patient_age=_str("PATIENT_AGE", ""),
            emergency_number=_str("EMERGENCY_NUMBER", "131"),
            browser_headless=_resolve_headless(_str("BROWSER_HEADLESS", "false")),
            browser_slow_mo_ms=_int("BROWSER_SLOW_MO_MS", 300),
            browser_keep_open_s=_int("BROWSER_KEEP_OPEN_S", 3),
            agent_max_steps=_int("AGENT_MAX_STEPS", 25),
            agent_timeout_s=_int("AGENT_TIMEOUT_S", 240),
            timezone=_str("TIMEZONE", "America/Santiago"),
            data_dir=Path(_str("DATA_DIR", str(ROOT / "data"))),
        )
