"""Prueba el agente Playwright en un Chromium real contra un portal de mentira (tests/fake_portal),
con un 'Claude' guionado que lee la misma lista numerada de elementos que vería el modelo.

El flujo del portal falso: Agendar hora -> buscar (especialidad + fecha) -> lista de horas -> datos del paciente -> confirmación.
"""

import functools
import http.server
import re
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import Error as PlaywrightError

from app.booking import Slot
from app.browser_agent import ClinicAgent, ClinicRequest, format_snapshot
from app.tasks import slot_text

PORTAL_DIR = Path(__file__).parent / "fake_portal"
_ids = [0]


@pytest.fixture(scope="module")
def portal_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PORTAL_DIR))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def use(name, **inp):
    _ids[0] += 1
    return NS(type="tool_use", id=f"toolu_{_ids[0]}", name=name, input=inp)


def find(snapshot: str, kind: str, label: str) -> int:
    """Id del elemento de la lista numerada cuyo tipo y nombre coinciden."""
    for line in snapshot.splitlines():
        m = re.match(r'\[(\d+)\] (\S+) "([^"]*)"', line)
        if m and kind in m.group(2) and label.lower() in m.group(3).lower():
            return int(m.group(1))
    raise AssertionError(f"no encontré {kind} '{label}' en:\n{snapshot}")


def texts_of(message) -> list[str]:
    content = message["content"]
    return [content] if isinstance(content, str) else [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]


class ScriptedClaude:
    """Guion según la fase (que lee del texto de la instrucción) y la página en la que está."""

    def __init__(self):
        self.messages = NS(create=self.create)
        self.requests: list[dict] = []
        self.snapshots: list[list[dict]] = []  # copia del historial en cada llamada

    def phase(self, messages) -> tuple[str, str]:
        for m in reversed(messages):
            if m["role"] != "user":
                continue
            for t in texts_of(m):
                for tag, name in (("FASE DE BÚSQUEDA", "search"), ("FASE DE RESERVA", "book"), ("TRÁMITE:", "oneshot")):
                    if t.startswith(tag):
                        return name, t
        raise AssertionError("no encontré la instrucción de la fase")

    async def create(self, **kw):
        self.requests.append(kw)
        self.snapshots.append(list(kw["messages"]))
        return NS(stop_reason="tool_use", content=self.decide(kw["messages"]))

    def decide(self, messages):
        phase, instruction = self.phase(messages)
        snap = texts_of(messages[-1])[-1]
        url = re.search(r"URL: (\S+)", snap).group(1)

        if phase == "oneshot":
            return [use("finish", exito=True, resumen="Tiene una hora el 2 de octubre.", comprobante="")]
        if "/confirmacion.html" in url:
            return [use("finish", exito=True, resumen="Hora reservada.", comprobante=re.search(r"Código CONF-\d+", snap).group(0))]
        if "/datos.html" in url:
            assert phase == "book", "la fase de búsqueda nunca debe llegar al formulario de datos"
            who = re.search(r"nombre completo: ([^;]+); RUT: ([^;]+); teléfono: ([^\n]+)", instruction)
            return [
                use("type_text", id=find(snap, "campo", "Nombre"), text=who.group(1)),
                use("type_text", id=find(snap, "campo", "RUT"), text=who.group(2)),
                use("type_text", id=find(snap, "campo", "Teléfono"), text=who.group(3)),
                use("click", id=find(snap, "botón", "Confirmar reserva")),
            ]
        if "/horas.html" in url:
            if phase == "search":
                slots = []
                for line in snap.splitlines():
                    m = re.match(r'\[\d+\] enlace "([^"]+?(\d\d:\d\d) - ([^"]+))"', line)
                    if m:
                        slots.append({"descripcion": m.group(1), "fecha": "2026-10-02", "hora": m.group(2), "profesional": m.group(3)})
                return [use("report_slots", slots=slots, resumen=f"Hay {len(slots)} horas.")]
            hora = re.search(r"hora (\d\d:\d\d)", instruction).group(1)
            return [use("click", id=find(snap, "enlace", hora))]
        if "/buscar.html" in url:
            return [
                use("select_option", id=find(snap, "lista", "Especialidad"), option="Cardiología"),
                use("type_text", id=find(snap, "campo[date]", "Fecha"), text="2026-10-02"),
                use("click", id=find(snap, "botón", "Buscar horas")),
            ]
        return [use("click", id=find(snap, "enlace", "Agendar"))]


@pytest.fixture
def agent_settings(settings, portal_url):
    return replace(settings, clinic_portal_url=portal_url)


REQUEST = ClinicRequest(tramite="agendar_hora", especialidad="Cardiología", fecha_preferida="2026-10-02",
                        horario_preferido="en la mañana", user_request="quiero hora con el cardiólogo")
PATIENT = {"nombre": "Rosa Muñoz Pérez", "rut": "7.654.321-K", "telefono": "+56 9 1234 5678"}
CHOSEN = Slot(2, "viernes, 2 de octubre, 10:30 - Dr. Pérez", "2026-10-02", "10:30", "Dr. Pérez")


async def test_search_then_book_in_the_same_open_browser(agent_settings):
    claude = ScriptedClaude()
    session = ClinicAgent(agent_settings, claude).new_session("run1")
    await session.open()
    try:
        # Fase 1: busca y se detiene en la lista de horas, SIN reservar nada
        found = await session.search(REQUEST, {})
        assert found.kind == "slots" and found.success and found.steps == 3
        assert [s["hora"] for s in found.slots] == ["09:30", "10:30", "11:15"]
        assert found.slots[1]["profesional"] == "Dr. Pérez" and "octubre" in found.slots[1]["descripcion"]
        assert session.is_open and session.page.url.endswith("/horas.html?esp=car&fecha=2026-10-02")
        assert Path(found.screenshot).exists()

        # Fase 2: la persona eligió y confirmó -> el MISMO navegador y el mismo historial completan la reserva
        done = await session.book(slot_text(CHOSEN), PATIENT)
        assert done.kind == "done" and done.success and done.confirmation == "Código CONF-4821"
        booked = parse_qs(urlparse(done.final_url).query)
        assert urlparse(done.final_url).path == "/confirmacion.html"
        assert booked["hora"] == ["10:30"] and booked["prof"] == ["Dr. Pérez"]  # la hora elegida, no otra
        assert booked["nombre"] == ["Rosa Muñoz Pérez"] and booked["rut"] == ["7.654.321-K"] and booked["telefono"] == ["+56 9 1234 5678"]
    finally:
        await session.close()

    # El historial es continuo: la herramienta terminal de la fase 1 recibió su tool_result al empezar la fase 2
    book_start = next(m for m in claude.snapshots[len(claude.snapshots) - 1] if m["role"] == "user" and any(t.startswith("FASE DE RESERVA") for t in texts_of(m)))
    assert book_start["content"][0]["type"] == "tool_result" and book_start["content"][0]["content"] == "ok"
    first = claude.requests[0]
    assert first["output_config"] == {"effort": "medium"} and first["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == ["click", "type_text", "select_option", "press_key", "go_to", "report_slots", "need_info", "finish"]
    assert "NO reserves" in texts_of(claude.snapshots[0][0])[0]


async def test_book_from_scratch_when_the_session_is_gone(agent_settings):
    claude = ScriptedClaude()
    session = ClinicAgent(agent_settings, claude).new_session("run2")
    await session.open()
    try:
        done = await session.book(slot_text(CHOSEN), PATIENT, request=REQUEST, from_scratch=True)
    finally:
        await session.close()
    assert done.success
    booked = parse_qs(urlparse(done.final_url).query)
    assert booked["hora"] == ["10:30"] and booked["prof"] == ["Dr. Pérez"]
    instruction = texts_of(claude.snapshots[0][0])[0]
    assert "busca de nuevo" in instruction and "Cardiología" in instruction


async def test_need_info_pauses_and_resumes(agent_settings):
    class AsksRut(ScriptedClaude):
        asked = False

        def decide(self, messages):
            if not self.asked:
                self.asked = True
                return [use("need_info", dato_faltante="RUT", pregunta="¿Me dice su RUT para mostrarle las horas?")]
            last = messages[-1]["content"]
            assert last[0]["type"] == "tool_result"  # la herramienta need_info recibe su resultado...
            assert "7.654.321-K" in last[1]["text"]  # ...y el dato llega en el mensaje de la fase siguiente
            return [use("report_slots", slots=[], resumen="No hay horas.")]

    claude = AsksRut()
    session = ClinicAgent(agent_settings, claude).new_session("run3")
    await session.open()
    try:
        asked = await session.search(REQUEST, {})
        assert asked.kind == "need_info" and asked.question.startswith("¿Me dice su RUT") and session.is_open
        resumed = await session.provide_info("RUT 7.654.321-K")
        assert resumed.kind == "slots" and resumed.slots == []
    finally:
        await session.close()


async def test_oneshot_run_opens_and_closes_the_browser(agent_settings):
    request = ClinicRequest(tramite="consultar_horas", user_request="¿qué horas tengo?")
    outcome = await ClinicAgent(agent_settings, ScriptedClaude()).run(request, "run4", PATIENT)
    assert outcome.success and outcome.summary == "Tiene una hora el 2 de octubre."


async def test_agent_cannot_leave_the_portal(agent_settings):
    class Probe(ScriptedClaude):
        probed = False

        def decide(self, messages):
            if not self.probed:
                self.probed = True
                snap = texts_of(messages[-1])[-1]
                return [use("go_to", path="http://example.com/"), use("click", id=find(snap, "enlace", "Agendar"))]
            return super().decide(messages)

    claude = Probe()
    session = ClinicAgent(agent_settings, claude).new_session("run5")
    await session.open()
    try:
        outcome = await session.search(REQUEST, {})
    finally:
        await session.close()
    assert outcome.kind == "slots"
    # go_to externo volvió como error y no movió la página; el clic siguiente se ejecutó igual
    results = claude.snapshots[1][2]["content"]
    assert results[0]["is_error"] and "dentro del portal" in results[0]["content"]


async def test_portal_down_raises_on_open_and_oneshot_reports_failure(settings):
    down = replace(settings, clinic_portal_url="http://127.0.0.1:9")
    agent = ClinicAgent(down, ScriptedClaude())
    with pytest.raises(PlaywrightError):
        await agent.new_session("run6").open()
    outcome = await agent.run(ClinicRequest(tramite="consultar_horas"), "run7")
    assert not outcome.success and "No se pudo operar el portal" in outcome.summary


async def test_step_limit_marks_the_session_unusable(agent_settings):
    class Lost(ScriptedClaude):
        def decide(self, messages):
            return [use("press_key", key="Tab")]

    session = ClinicAgent(replace(agent_settings, agent_max_steps=3), Lost()).new_session("run8")
    await session.open()
    try:
        outcome = await session.search(REQUEST, {})
        assert not outcome.success and "3 pasos" in outcome.summary
        assert not session.is_open  # no se reutiliza un historial a medias
    finally:
        await session.close()


async def test_terminal_tool_alongside_actions_is_rejected(agent_settings):
    class Eager(ScriptedClaude):
        def decide(self, messages):
            if len(self.requests) == 1:
                snap = texts_of(messages[-1])[-1]
                return [use("click", id=find(snap, "enlace", "Agendar")), use("report_slots", slots=[], resumen="ya")]
            return [use("report_slots", slots=[], resumen="me detengo")]

    claude = Eager()
    session = ClinicAgent(agent_settings, claude).new_session("run9")
    await session.open()
    try:
        outcome = await session.search(REQUEST, {})
    finally:
        await session.close()
    results = claude.snapshots[1][2]["content"]
    assert results[1]["is_error"] and "sola" in results[1]["content"]
    assert outcome.kind == "slots" and outcome.summary == "me detengo"


def test_format_snapshot_is_compact_and_numbered():
    snap = {"url": "http://x/", "title": "T", "text": "Hola", "truncated": True, "items": [
        {"id": 1, "tag": "a", "type": "", "label": "", "name": "", "placeholder": "", "disabled": False, "text": "Agendar", "href": "/agendar"},
        {"id": 2, "tag": "select", "type": "", "label": "Especialidad", "name": "", "placeholder": "", "disabled": False, "options": ["A", "B"], "value": "A"},
        {"id": 3, "tag": "input", "type": "checkbox", "label": "Acepto", "name": "", "placeholder": "", "disabled": True, "value": "on", "checked": False},
    ]}
    out = format_snapshot(snap)
    assert '[1] enlace "Agendar" -> /agendar' in out
    assert "[2] lista \"Especialidad\" opciones=['A', 'B'] valor=\"A\"" in out
    assert "[3] campo[checkbox] \"Acepto\" valor=\"on\" sin marcar (deshabilitado)" in out
    assert "no se listan" in out
