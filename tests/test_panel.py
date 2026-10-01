"""El panel desplegado recibe POST /api/logs con su propio esquema (ver app/panel.py)."""

import pytest

from app.panel import appointment_due_at, confirmation_code, panel_elder_name, panel_endpoint, scrub, to_panel_log
from app.schemas import CaregiverEvent

TZ = "America/Santiago"
KNOWN_ACTIONS = {"ejecutar_tramite_web", "enviar_mensaje_familiar", "reporte_diario_y_alerta_sintomas"}
KNOWN_CATEGORIES = {"cita_medica", "tramite", "mensaje", "recordatorio", "medicacion", "salud", "alerta", "digest", "navegacion", "general"}


def event(event_type="clinic_task", status="success", severity="info", summary="Resumen.", **details) -> CaregiverEvent:
    return CaregiverEvent(event_id="ev1", event_type=event_type, status=status, severity=severity, timestamp="2026-10-01T12:00:00-03:00",
                          elder_name="don René", summary=summary, user_request="sí, confírmela", details=details)


BOOKED = dict(task="agendar_hora", specialty="cardiología", confirmation="Código CONF-4821",
              appointment={"option": 2, "description": "viernes, 10:30 - Dr. Pérez", "date": "2026-10-02", "time": "10:30", "professional": "Dr. Pérez"},
              patient={"name": "René Soto", "rut": "7.654.321-K", "phone": "+56 9 1234 5678"}, screenshot="/home/x/data/s.png", final_url="http://localhost:8080/ok")


def test_booked_appointment_becomes_a_cita_medica_with_a_reminder():
    log = to_panel_log(event(summary="Quedó confirmada para el viernes 2 a las 10:30.", **BOOKED), "Andrea", TZ)
    assert (log["category"], log["action"], log["status"]) == ("cita_medica", "ejecutar_tramite_web", "ok")
    assert log["title"] == "Hora médica de Cardiología" and log["message"] == "Quedó confirmada para el viernes 2 a las 10:30."
    assert log["elder"] == "René" and log["source"] == "agente_llm" and log["actor"] == "Asistente de voz"
    assert log["due_at"] == "2026-10-02T10:30:00-03:00" and log["create_reminder"] is True
    assert log["metadata"]["profesional"] == "Dr. Pérez" and log["metadata"]["codigo"] == "CONF-4821" and log["metadata"]["event_id"] == "ev1"


def test_panel_is_public_so_no_rut_phone_or_local_paths_are_sent():
    flat = str(to_panel_log(event(**BOOKED), "Andrea", TZ))
    for secret in ("7.654.321-K", "+56 9 1234", "/home/x", "René Soto", "screenshot", "localhost"):
        assert secret not in flat


def test_confirmation_screen_that_repeats_the_patient_data_only_sends_the_code():
    # La pantalla real del portal repite nombre, RUT y teléfono junto al código (caso que se coló en la primera prueba real).
    screen = "Código CONF-4821. Fecha 2026-10-02 a las 10:30 con Dr. Pérez. Paciente René Soto Díaz, RUT 12.345.678-5, teléfono 9 1234 5678."
    booked = {**BOOKED, "confirmation": screen}
    ev = event(summary="Reservada. Paciente René Soto Díaz, RUT 12345678-5, fono +56 9 8765 4321.", **booked)
    ev.redact = ["René Soto Díaz", "12.345.678-5", "9 1234 5678"]  # lo que la persona dijo por voz (lo marca el servidor)
    log = to_panel_log(ev, "Andrea", TZ)
    assert log["metadata"]["codigo"] == "CONF-4821"
    flat = str(log)
    for leaked in ("12.345.678-5", "12345678-5", "9 1234 5678", "8765 4321", "René Soto", "Díaz"):
        assert leaked not in flat, leaked
    assert log["elder"] == "René" and "[dato oculto]" in log["message"]  # el campo que usa el panel para asociar no se toca


def test_even_an_unmarked_rut_or_phone_in_the_summary_is_hidden():
    log = to_panel_log(event(summary="Quedó. RUT 12.345.678-5 y fono +56 9 8765 4321.", **BOOKED), "Andrea", TZ)
    assert "12.345.678-5" not in str(log) and "8765 4321" not in str(log)


def test_redacting_a_name_equal_to_the_elder_never_breaks_the_elder_field():
    ev = event(summary="Hora para René Soto.", **BOOKED)
    ev.redact = ["René"]
    assert to_panel_log(ev, "Andrea", TZ)["elder"] == "René"


def test_redact_list_is_not_serialized_into_the_event():
    ev = event(**BOOKED)
    ev.redact = ["12.345.678-5"]
    assert "redact" not in ev.model_dump(mode="json")


def test_scrub_hides_ruts_and_phones_but_not_dates_or_times():
    text = "RUT 7.654.321-K, otro 7654321-k, cel +56 9 1234 5678, 912345678, 9-1234-5678; cita 2026-10-02 a las 10:30, código CONF-4821."
    out = scrub(text)
    for hidden in ("7.654.321-K", "7654321-k", "1234 5678", "912345678", "9-1234-5678"):
        assert hidden not in out
    assert "2026-10-02" in out and "10:30" in out and "CONF-4821" in out
    assert scrub({"a": ["12.345.678-5", {"b": "ok"}], "n": 5}) == {"a": ["[dato oculto]", {"b": "ok"}], "n": 5}


def test_confirmation_code_extraction():
    assert confirmation_code("Código CONF-4821. Paciente X") == "CONF-4821"
    assert confirmation_code("Su reserva es RES123456") == "RES123456"
    assert confirmation_code("Hora confirmada, sin código") is None and confirmation_code("") is None


def test_no_reminder_when_the_portal_gave_no_valid_date():
    booked = {**BOOKED, "appointment": {"description": "mañana temprano", "date": "", "time": ""}}
    log = to_panel_log(event(**booked), "Andrea", TZ)
    assert log["due_at"] is None and log["create_reminder"] is False and log["category"] == "cita_medica"


def test_failed_booking_and_failed_search_are_alerts():
    failed = to_panel_log(event(status="error", severity="important", summary="Esa hora ya no estaba.", **BOOKED), "Andrea", TZ)
    assert (failed["category"], failed["status"], failed["title"]) == ("tramite", "alerta", "No se pudo agendar una hora")
    search = to_panel_log(event(status="error", severity="important", task="agendar_hora", stage="search"), "Andrea", TZ)
    assert search["title"] == "No se pudieron buscar horas" and search["metadata"]["stage"] == "search"


def test_consult_and_cancel_are_tramites():
    ok = to_panel_log(event(task="consultar_horas", summary="Tiene una hora el 2."), "Andrea", TZ)
    assert (ok["category"], ok["status"], ok["title"]) == ("tramite", "ok", "Trámite: consultar horas")


def test_caregiver_messages_alerts_and_emergencies():
    info = to_panel_log(event("caregiver_alert", "info", "info", "Quiere que lo llames.", urgency="info"), "Andrea", TZ)
    assert (info["category"], info["action"], info["status"], info["title"]) == ("mensaje", "enviar_mensaje_familiar", "ok", "Recado para Andrea")
    assert info["message"] == "«Quiere que lo llames.»" and info["metadata"]["destinatario"] == "Andrea"

    important = to_panel_log(event("caregiver_alert", "info", "important", "Se siente mareado.", urgency="importante"), "Andrea", TZ)
    assert (important["category"], important["status"], important["title"]) == ("alerta", "alerta", "Aviso importante de René")

    emergency = to_panel_log(event("emergency", "info", "emergency", "Le duele el pecho.", urgency="emergencia"), "Andrea", TZ)
    assert (emergency["category"], emergency["status"]) == ("alerta", "alerta") and "Emergencia de René" in emergency["title"]
    assert emergency["metadata"]["urgencia"] == "emergencia"


def test_system_error_is_an_alert_with_the_stage():
    log = to_panel_log(event("system_error", "error", "important", "No se pudo transcribir.", stage="stt", error="APIError"), "Andrea", TZ)
    assert (log["category"], log["status"]) == ("alerta", "alerta") and log["metadata"]["stage"] == "stt"


@pytest.mark.parametrize("kwargs", [
    dict(**BOOKED), dict(task="consultar_horas"), dict(status="error", **BOOKED),
    dict(event_type="caregiver_alert", urgency="info"), dict(event_type="emergency", severity="emergency"), dict(event_type="system_error", status="error"),
])
def test_every_log_only_uses_values_the_panel_knows(kwargs):
    log = to_panel_log(event(**kwargs), "Andrea", TZ)
    assert log["category"] in KNOWN_CATEGORIES and log["action"] in KNOWN_ACTIONS and log["status"] in {"ok", "info", "alerta"}
    assert log["title"] and log["message"] and log["elder"] == "René"


def test_panel_endpoint_appends_api_logs_only_to_a_bare_site_url():
    assert panel_endpoint("https://panel-cuidador.vercel.app/") == "https://panel-cuidador.vercel.app/api/logs"
    assert panel_endpoint("https://panel-cuidador.vercel.app") == "https://panel-cuidador.vercel.app/api/logs"
    assert panel_endpoint("http://localhost:3000/api/logs") == "http://localhost:3000/api/logs"
    assert panel_endpoint("http://localhost:3000/api/events") == "http://localhost:3000/api/events"


def test_elder_name_without_honorific_and_due_at_parsing():
    assert [panel_elder_name(n) for n in ("don René", "doña Rosa", "René", " Don Luis ")] == ["René", "Rosa", "René", "Luis"]
    assert appointment_due_at({"date": "2026-10-02", "time": "10:30"}, TZ) == "2026-10-02T10:30:00-03:00"
    assert appointment_due_at({"date": "2026-10-02", "time": "10:30"}, "Nowhere/Invalid").startswith("2026-10-02T10:30:00")
    assert appointment_due_at({"date": "hoy", "time": ""}, TZ) is None
