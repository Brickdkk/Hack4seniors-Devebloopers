import json

import httpx
import pytest

from app.dispatcher import CaregiverDispatcher
from app.schemas import CaregiverEvent


def make_event() -> CaregiverEvent:
    return CaregiverEvent(
        event_id="abc123",
        event_type="clinic_task",
        status="success",
        timestamp="2026-10-01T12:00:00-03:00",
        elder_name="doña Rosa",
        summary="Hora agendada con cardiología.",
        user_request="quiero una hora con el cardiólogo",
        details={"task": "agendar_hora", "specialty": "cardiología", "confirmation": "CONF-1",
                 "appointment": {"date": "2026-10-02", "time": "10:30", "professional": "Dr. Pérez"}},
    )


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("app.dispatcher.BACKOFF_S", 0)


def outbox_lines(settings):
    path = settings.data_dir / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_posts_json_payload_and_logs_outbox(settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200)

    d = CaregiverDispatcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.send(make_event()) is True

    url, body = seen[0]
    assert url == "http://panel.test/api/logs"  # una URL con ruta se respeta tal cual
    assert (body["category"], body["status"], body["elder"]) == ("cita_medica", "ok", "Rosa")
    assert body["due_at"] == "2026-10-02T10:30:00-03:00" and body["create_reminder"] is True
    line = outbox_lines(settings)[0]
    assert line["delivered"] is True and line["event"]["event_id"] == "abc123" and line["panel_payload"] == body


async def test_bare_site_url_posts_to_api_logs_and_records_the_panel_log_id(settings):
    from dataclasses import replace

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(201, json={"id": 42, "category": "cita_medica", "title": "Hora médica de Cardiología"})

    d = CaregiverDispatcher(replace(settings, caregiver_panel_url="https://panel.test/"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.send(make_event()) is True
    assert seen == ["https://panel.test/api/logs"]
    assert outbox_lines(settings)[0]["panel_log_id"] == 42


async def test_retries_server_errors_then_succeeds(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200)

    d = CaregiverDispatcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.send(make_event()) is True
    assert len(calls) == 3


async def test_client_error_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(422)

    d = CaregiverDispatcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.send(make_event()) is False
    assert len(calls) == 1
    line = outbox_lines(settings)[0]
    assert line["delivered"] is False and line["error"] == "HTTP 422"


async def test_unreachable_panel_keeps_event_in_outbox(settings):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    d = CaregiverDispatcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await d.send(make_event()) is False
    line = outbox_lines(settings)[0]
    assert line["delivered"] is False and "ConnectError" in line["error"]
    assert line["event"]["event_id"] == "abc123" and line["panel_payload"]["title"]
