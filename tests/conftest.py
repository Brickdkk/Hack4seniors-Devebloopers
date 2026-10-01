from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)  # los tests no leen el .env real
    return replace(
        Settings.from_env(),
        claude_model="claude-sonnet-5-5",
        claude_effort="low",
        agent_effort="medium",
        refusal_fallback=True,
        elder_name="doña Rosa",
        caregiver_name="Camila",
        emergency_number="131",
        timezone="America/Santiago",
        patient_full_name="",
        patient_rut="",
        patient_phone="",
        patient_age="",
        data_dir=tmp_path,
        clinic_portal_url="http://127.0.0.1:0",
        caregiver_panel_url="http://panel.test/api/logs",
        browser_headless=True,
        browser_slow_mo_ms=0,
        browser_keep_open_s=0,
        agent_max_steps=8,
        agent_timeout_s=60,
    )
