import asyncio
import json
import time
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.brain import BrainResult, ToolCall
from app.browser_agent import AgentOutcome
from app.dispatcher import CaregiverDispatcher
from app.main import create_app
from app.pipeline import Services, VoicePipeline
from app.speech import SpeechQueue
from app.tasks import ASK_DATA, TaskManager

WAV = b"RIFF....WAVEfmt fake-audio"
SLOTS = [
    {"descripcion": "viernes 2 de octubre, 09:30 - Dra. Soto", "fecha": "2026-10-02", "hora": "09:30", "profesional": "Dra. Soto"},
    {"descripcion": "viernes 2 de octubre, 10:30 - Dr. Pérez", "fecha": "2026-10-02", "hora": "10:30", "profesional": "Dr. Pérez"},
]
RAW_PATIENT = dict(nombre_completo="Rosa Muñoz Pérez", rut="7.654.321-K", telefono="+56 9 1234 5678")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("app.dispatcher.BACKOFF_S", 0)


# -- dobles de prueba --------------------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self, text="Quiero una hora con el cardiólogo"):
        self.text = text

    async def transcribe(self, audio, filename="audio.wav"):
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


class FakeBrain:
    """think() entrega los resultados en orden (repite el último); follow_up() entrega las frases de aviso."""

    def __init__(self, *results, follow_ups=(), error=None):
        self.results, self.follow_ups, self.error = list(results), list(follow_ups), error
        self.seen, self.notices = [], []

    async def think(self, transcript, context="(sin novedades)"):
        self.seen.append((transcript, context))
        if self.error:
            raise self.error
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    async def follow_up(self, notice, context="(sin novedades)"):
        self.notices.append((notice, context))
        return BrainResult(self.follow_ups.pop(0) if self.follow_ups else "Aviso.")


class FakeSynth:
    def __init__(self, fail=False):
        self.fail, self.spoken = fail, []

    async def synthesize(self, text):
        if self.fail:
            raise RuntimeError("sin red")
        self.spoken.append(text)
        return b"AUDIO"


class FakeSession:
    """Sesión del portal guionada: cada fase devuelve el siguiente resultado de su lista."""

    def __init__(self, search=(), book=(), info=()):
        self.script = {"search": list(search), "book": list(book), "info": list(info)}
        self.calls, self.closed, self.opened = [], False, False
        self.idle_s = 0.0

    @property
    def is_open(self):
        return self.opened and not self.closed

    async def open(self):
        self.opened = True

    async def close(self, linger=False):
        self.closed = True

    async def search(self, request, patient, continuing=False):
        self.calls.append(("search", request, dict(patient), continuing))
        return self.script["search"].pop(0)

    async def provide_info(self, info):
        self.calls.append(("info", info))
        return self.script["info"].pop(0)

    async def book(self, slot_text, patient, extra="", request=None, from_scratch=False):
        self.calls.append(("book", slot_text, dict(patient), extra, from_scratch))
        return self.script["book"].pop(0)


def slots_outcome(slots=SLOTS):
    return AgentOutcome(True, f"Hay {len(slots)} horas.", kind="slots", slots=[dict(s) for s in slots], steps=3)


BOOKED = AgentOutcome(True, "Hora reservada para el viernes 2 de octubre a las 10:30 con el Dr. Pérez.", "Código CONF-4821", steps=4, final_url="http://portal/confirmacion")


class FakeClinicAgent:
    def __init__(self, *sessions, oneshot=None):
        self.sessions, self.oneshot, self.oneshot_calls, self.created = list(sessions), oneshot, [], []

    def new_session(self, run_id):
        session = self.sessions.pop(0) if len(self.sessions) > 1 else self.sessions[0]
        self.created.append(run_id)
        return session

    async def run(self, request, run_id, patient=None):
        self.oneshot_calls.append((request, run_id, patient))
        return self.oneshot


def build(settings, *, transcriber=None, brain=None, synth=None, agent=None, panel_status=200):
    posts = []

    def panel(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(panel_status)

    dispatcher = CaregiverDispatcher(settings, httpx.AsyncClient(transport=httpx.MockTransport(panel)))
    agent = agent or FakeClinicAgent(FakeSession())
    synthesizer = synth or FakeSynth()
    services = Services(
        settings=settings,
        transcriber=transcriber or FakeTranscriber(),
        brain=brain or FakeBrain(BrainResult("Listo, doña Rosa.")),
        synthesizer=synthesizer,
        dispatcher=dispatcher,
        clinic_agent=agent,
        tasks=TaskManager(settings, dispatcher, agent),
        speech=SpeechQueue(synthesizer),
    )
    VoicePipeline(services)  # conecta el anunciador de la cola de voz
    return services, posts


def call(name, **inp):
    inp.setdefault("frase_hablada", "Un momento.")
    return ToolCall(f"toolu_{name}", name, inp)


SEARCH = call("buscar_horas", especialidad="Cardiología", fecha_preferida="2026-10-02", horario_preferido="en la mañana", detalles="", datos_paciente="")
PROPOSE = call("proponer_reserva", opcion=2, frase_hablada="¿Le confirmo la hora con el doctor Pérez?", **RAW_PATIENT)
BOOK = call("reservar_hora", opcion=2, datos_extra="")
ALERT = call("avisar_al_cuidador", urgencia="emergencia", mensaje="Le duele el pecho.", frase_hablada="Llame al 131.")


async def run(services, *calls, request="quiero una hora"):
    """Lanza herramientas como lo haría el pipeline y espera a que terminen las tareas."""
    ids, reply = services.tasks.dispatch(list(calls), request, "frase del modelo")
    await services.tasks.wait_all()
    return ids, reply


# -- flujo de agendar: de la búsqueda a la reserva -----------------------------------------------------


async def test_search_announces_the_slots_by_voice(settings):
    session = FakeSession(search=[slots_outcome()])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=FakeBrain(follow_ups=["Hay dos horas: ¿cuál prefiere?"]))
    ids, reply = await run(services, SEARCH)

    assert reply == "frase del modelo" and len(ids) == 1
    task = services.tasks.get(ids[0])
    assert task.status == "success" and [s["time"] for s in task.result["slots"]] == ["09:30", "10:30"]
    kind, request, patient, continuing = session.calls[0]
    assert (kind, request.especialidad, request.fecha_preferida, continuing) == ("search", "Cardiología", "2026-10-02", False)
    assert services.tasks.booking.phase == "slots" and posts == []  # buscar no avisa al panel: todavía no pasó nada que contar

    notice, context = services.brain.notices[0]  # Claude recibió el resultado y las horas numeradas en el contexto
    assert "2 horas disponibles" in notice
    assert "1) viernes 2 de octubre, 09:30 - Dra. Soto" in context and "2) viernes 2 de octubre, 10:30 - Dr. Pérez" in context
    item, audio = await services.speech.pop()
    assert item.text == "Hay dos horas: ¿cuál prefiere?" and audio == b"AUDIO"


async def test_full_booking_flow_dispatches_confirmed_appointment_to_panel(settings):
    session = FakeSession(search=[slots_outcome()], book=[BOOKED])
    brain = FakeBrain(follow_ups=["Hay dos horas.", "Listo, su hora quedó reservada."])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=brain)
    tasks = services.tasks

    await run(services, SEARCH)
    assert (await services.speech.pop()) is not None

    ids, reply = await run(services, PROPOSE)  # la persona eligió la 2 y dio sus datos: se le pide confirmación
    assert reply == "frase del modelo" and tasks.get(ids[0]).summary.startswith("Esperando")
    assert tasks.booking.valid_pending().option == 2 and tasks.booking.patient["rut"] == "7.654.321-K"
    assert "Esperando que la persona confirme con un sí: opción 2" in tasks.context()
    assert not any(c[0] == "book" for c in session.calls)  # aún no se reservó nada

    ids, _ = await run(services, BOOK, request="sí, confírmela")  # dijo que sí
    book = next(c for c in session.calls if c[0] == "book")
    assert "10:30 - Dr. Pérez" in book[1] and book[2]["nombre"] == "Rosa Muñoz Pérez" and book[4] is False

    log = posts[0]  # lo que recibe el panel desplegado (POST /api/logs)
    assert (log["category"], log["action"], log["status"], log["title"]) == ("cita_medica", "ejecutar_tramite_web", "ok", "Hora médica de Cardiología")
    assert log["message"] == BOOKED.summary and log["elder"] == "Rosa" and log["source"] == "agente_llm"
    assert log["due_at"] == "2026-10-02T10:30:00-03:00" and log["create_reminder"] is True
    assert log["metadata"]["profesional"] == "Dr. Pérez" and log["metadata"]["codigo"] == "CONF-4821" and log["metadata"]["event_type"] == "clinic_task"
    assert "7.654.321-K" not in str(log) and "Rosa Muñoz" not in str(log)  # el panel es público: sin RUT ni datos personales

    assert "quedó reservada" in brain.notices[1][0] and "CONF-4821" in brain.notices[1][0]
    assert (await services.speech.pop())[0].text == "Listo, su hora quedó reservada."
    assert tasks.booking.phase == "idle" and tasks.booking.patient["rut"] == "7.654.321-K"  # los datos quedan para la próxima vez
    assert session.closed


# -- salvaguardas: nunca se reserva sin confirmación ---------------------------------------------------


async def test_booking_without_a_confirmed_proposal_asks_instead_of_booking(settings):
    session = FakeSession(search=[slots_outcome()], book=[BOOKED])
    services, posts = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)

    ids, reply = await run(services, BOOK)  # el modelo se adelanta: sin proponer_reserva ni datos
    assert ids == [] and reply == ASK_DATA
    assert not any(c[0] == "book" for c in session.calls) and posts == []

    services.tasks.booking.update_patient(**{"nombre": "Rosa Muñoz", "rut": "7.654.321-K"})  # ya tiene los datos pero no el sí
    ids, reply = await run(services, BOOK)
    assert ids == [] and "¿me confirma que quiere la hora viernes 2 de octubre, 10:30 - Dr. Pérez" in reply and "Rosa Muñoz" in reply
    assert services.tasks.booking.valid_pending().option == 2  # ahora sí espera el "sí"
    assert not any(c[0] == "book" for c in session.calls)

    ids, _ = await run(services, BOOK)  # con el sí, esta vez se ejecuta
    assert len(ids) == 1 and any(c[0] == "book" for c in session.calls)


async def test_confirmation_for_another_option_does_not_book(settings):
    session = FakeSession(search=[slots_outcome()], book=[BOOKED])
    services, _ = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)
    await run(services, PROPOSE)  # propuso la 2
    ids, reply = await run(services, call("reservar_hora", opcion=1, datos_extra=""))  # pero "confirma" la 1
    assert ids == [] and "Dra. Soto" in reply and not any(c[0] == "book" for c in session.calls)


async def test_stale_proposal_expires(settings, monkeypatch):
    session = FakeSession(search=[slots_outcome()], book=[BOOKED])
    services, _ = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)
    await run(services, PROPOSE)
    monkeypatch.setattr("app.booking.PENDING_TTL_S", -1)
    ids, reply = await run(services, BOOK)
    assert ids == [] and "¿me confirma" in reply and not any(c[0] == "book" for c in session.calls)


async def test_proposal_needs_name_and_rut_and_a_valid_option(settings):
    services, _ = build(settings, agent=FakeClinicAgent(FakeSession(search=[slots_outcome()])))
    ids, reply = await run(services, call("proponer_reserva", opcion=1, nombre_completo="", rut="", telefono=""))
    assert ids == [] and "no tengo horas" in reply.lower()  # todavía no hay horas

    await run(services, SEARCH)
    ids, reply = await run(services, call("proponer_reserva", opcion=1, nombre_completo="Rosa Muñoz", rut="", telefono=""))
    assert ids == [] and reply == ASK_DATA and services.tasks.booking.valid_pending() is None
    ids, reply = await run(services, call("proponer_reserva", opcion=9, **RAW_PATIENT))
    assert ids == [] and "no encuentro esa hora" in reply


async def test_saved_profile_fills_everything_and_never_reaches_the_panel(settings):
    from dataclasses import replace

    saved = replace(settings, patient_full_name="Mario Quiroga Díaz", patient_rut="12.345.678-5", patient_phone="", patient_age="65", elder_name="don Eliecer")
    confirmation = "Código CONF-4821. Paciente Mario Quiroga Díaz, RUT 12.345.678-5."
    session = FakeSession(search=[slots_outcome()], book=[AgentOutcome(True, "Reservada para Mario Quiroga Díaz.", confirmation, final_url="http://portal/ok")])
    services, posts = build(saved, agent=FakeClinicAgent(session))
    await run(services, SEARCH)

    context = services.tasks.context()
    assert "nombre completo: Mario Quiroga Díaz, RUT: 12.345.678-5, edad: 65 años" in context
    assert "datos GUARDADOS" in context and "Datos obligatorios que aún no tienes" not in context  # el teléfono no es obligatorio

    ids, _ = await run(services, call("proponer_reserva", opcion=2, nombre_completo="", rut="", telefono="", frase_hablada="Relleno todo con sus datos guardados."))
    assert len(ids) == 1  # no se pidió nada: bastan los datos guardados
    await run(services, BOOK)
    patient = next(c for c in session.calls if c[0] == "book")[2]
    assert patient == {"nombre": "Mario Quiroga Díaz", "rut": "12.345.678-5", "edad": "65"}  # lo que recibe el agente del portal

    log = posts[0]
    assert log["category"] == "cita_medica" and log["elder"] == "Eliecer" and log["metadata"]["codigo"] == "CONF-4821"
    assert "12.345.678-5" not in str(log) and "Quiroga" not in str(log)  # el panel es público


async def test_voice_patient_data_is_still_collected_when_nothing_is_saved(settings):
    services, _ = build(settings, agent=FakeClinicAgent(FakeSession(search=[slots_outcome()])))
    await run(services, SEARCH)
    assert "Datos obligatorios que aún no tienes: nombre completo, RUT" in services.tasks.context()
    assert "GUARDADOS" not in services.tasks.context()


# -- fallas y casos borde del portal -------------------------------------------------------------------


async def test_booking_failure_is_reported_and_offered_again(settings):
    failed = AgentOutcome(False, "Esa hora ya no estaba disponible.")
    session = FakeSession(search=[slots_outcome()], book=[failed])
    brain = FakeBrain(follow_ups=["Hay dos horas.", "Esa hora se ocupó, ¿buscamos otra?"])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=brain)
    await run(services, SEARCH)
    await run(services, PROPOSE)
    ids, _ = await run(services, BOOK)

    assert services.tasks.get(ids[0]).status == "error"
    assert (posts[0]["category"], posts[0]["status"], posts[0]["message"]) == ("tramite", "alerta", "Esa hora ya no estaba disponible.")
    assert "No se pudo reservar" in brain.notices[1][0] and services.tasks.booking.phase == "idle" and session.closed


async def test_search_failure_resets_and_notifies(settings):
    session = FakeSession(search=[AgentOutcome(False, "No se pudo abrir el portal de la clínica: ERR_CONNECTION_REFUSED")])
    brain = FakeBrain(follow_ups=["No pude entrar al sistema de la clínica."])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=brain)
    ids, _ = await run(services, SEARCH)
    assert services.tasks.get(ids[0]).status == "error" and services.tasks.booking.phase == "idle" and session.closed
    assert posts[0]["metadata"]["stage"] == "search" and posts[0]["status"] == "alerta" and posts[0]["title"] == "No se pudieron buscar horas"
    assert (await services.speech.pop())[0].text == "No pude entrar al sistema de la clínica."


async def test_no_available_slots_is_announced(settings):
    services, _ = build(settings, agent=FakeClinicAgent(FakeSession(search=[slots_outcome([])])))
    await run(services, SEARCH)
    assert "no hay horas disponibles" in services.brain.notices[0][0] and "no mostró horas" in services.brain.notices[0][1]


async def test_portal_asks_for_data_then_search_continues_in_same_session(settings):
    asks = AgentOutcome(True, "El portal necesita: RUT", kind="need_info", question="¿Me dice su RUT?")
    session = FakeSession(search=[asks], info=[slots_outcome()])
    services, _ = build(settings, agent=FakeClinicAgent(session), brain=FakeBrain(follow_ups=["¿Me dice su RUT?", "Hay dos horas."]))
    await run(services, SEARCH)
    assert services.tasks.booking.phase == "need_info" and "necesita este dato de la persona: ¿Me dice su RUT?" in services.tasks.context()

    again = call("buscar_horas", especialidad="Cardiología", fecha_preferida="2026-10-02", horario_preferido="", detalles="", datos_paciente="RUT 7.654.321-K")
    await run(services, again)
    assert session.calls[1] == ("info", "RUT 7.654.321-K") and services.tasks.booking.phase == "slots"
    assert len(services.clinic_agent.created) == 1  # misma sesión


async def test_new_search_reuses_the_open_session(settings):
    session = FakeSession(search=[slots_outcome(), slots_outcome(SLOTS[:1])])
    services, _ = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)
    await run(services, call("buscar_horas", especialidad="Cardiología", fecha_preferida="2026-10-03", horario_preferido="", detalles="", datos_paciente=""))
    assert len(services.clinic_agent.created) == 1 and session.calls[1][3] is True  # continuing
    assert len(services.tasks.booking.slots) == 1 and services.tasks.booking.pending is None


async def test_booking_without_live_session_searches_again(settings):
    first = FakeSession(search=[slots_outcome()])
    second = FakeSession(book=[BOOKED])
    services, posts = build(settings, agent=FakeClinicAgent(first, second))
    await run(services, SEARCH)
    first.opened = False  # el navegador se cerró o expiró entre turnos
    await run(services, PROPOSE)
    await run(services, BOOK)
    assert second.calls[0][0] == "book" and second.calls[0][4] is True  # from_scratch
    assert posts[0]["status"] == "ok"


async def test_portal_asks_extra_data_while_booking_restores_confirmation(settings):
    asks = AgentOutcome(True, "El portal necesita: email", kind="need_info", question="¿Cuál es su correo?")
    session = FakeSession(search=[slots_outcome()], book=[asks, BOOKED])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=FakeBrain(follow_ups=["Hay dos.", "¿Cuál es su correo?", "Listo."]))
    await run(services, SEARCH)
    await run(services, PROPOSE)
    await run(services, BOOK)
    assert posts == [] and services.tasks.booking.valid_pending().option == 2  # la confirmación ya dada sigue vigente
    ids, _ = await run(services, call("reservar_hora", opcion=2, datos_extra="correo rosa@mail.cl"))
    assert len(ids) == 1 and session.calls[-1][3] == "correo rosa@mail.cl" and posts[0]["status"] == "ok"


async def test_abandon_closes_the_browser_and_clears_state(settings):
    session = FakeSession(search=[slots_outcome()])
    services, posts = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)
    await run(services, PROPOSE)
    await run(services, call("abandonar_reserva"))
    assert session.closed and services.tasks.booking.phase == "idle" and services.tasks.booking.slots == []
    ids, reply = await run(services, BOOK)
    assert ids == [] and "no tengo horas" in reply.lower() and posts == []


async def test_double_booking_is_blocked_while_one_is_running(settings):
    gate = asyncio.Event()

    class Slow(FakeSession):
        async def book(self, *a, **k):
            await gate.wait()
            return BOOKED

    session = Slow(search=[slots_outcome()])
    services, posts = build(settings, agent=FakeClinicAgent(session))
    await run(services, SEARCH)
    await run(services, PROPOSE)
    ids, _ = services.tasks.dispatch([BOOK], "sí", "x")
    await asyncio.sleep(0.05)
    ids2, reply = services.tasks.dispatch([BOOK], "sí", "x")
    assert len(ids) == 1 and ids2 == [] and "Ya estoy reservando" in reply
    gate.set()
    await services.tasks.wait_all()
    assert len(posts) == 1


async def test_consult_oneshot_is_announced(settings):
    outcome = AgentOutcome(True, "Tiene una hora el 2 de octubre a las 10:30.", steps=2)
    agent = FakeClinicAgent(FakeSession(), oneshot=outcome)
    services, posts = build(settings, agent=agent, brain=FakeBrain(follow_ups=["Tiene hora el dos de octubre."]))
    ids, _ = await run(services, call("tramitar_en_clinica", tramite="consultar_horas", detalles=""))
    assert agent.oneshot_calls[0][0].tramite == "consultar_horas" and posts[0]["metadata"]["tramite"] == "consultar_horas"
    assert "Tiene una hora el 2 de octubre" in services.brain.notices[0][0]
    assert (await services.speech.pop())[0].text == "Tiene hora el dos de octubre."


async def test_unexpected_crash_resets_booking_and_tells_the_person(settings):
    class Boom(FakeSession):
        async def search(self, *a, **k):
            raise RuntimeError("boom")

    services, posts = build(settings, agent=FakeClinicAgent(Boom()), brain=FakeBrain(follow_ups=["Tuve un problema técnico."]))
    ids, _ = await run(services, SEARCH)
    assert services.tasks.get(ids[0]).status == "error" and services.tasks.booking.phase == "idle"
    assert posts[0]["metadata"]["event_type"] == "system_error" and (await services.speech.pop())[0].text == "Tuve un problema técnico."


async def test_announcement_may_only_alert_the_caregiver(settings):
    class Pushy(FakeBrain):
        async def follow_up(self, notice, context=""):
            return BrainResult("Aviso.", [BOOK, ALERT])  # un aviso automático no puede reservar

    session = FakeSession(search=[slots_outcome()], book=[BOOKED])
    services, posts = build(settings, agent=FakeClinicAgent(session), brain=Pushy())
    await run(services, SEARCH)
    await services.tasks.wait_all()
    assert not any(c[0] == "book" for c in session.calls)
    assert [p["metadata"]["event_type"] for p in posts] == ["emergency"]


# -- API HTTP ----------------------------------------------------------------------------------------------


def wait_for_task(client, task_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in ("success", "error"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"la tarea {task_id} no terminó: {task}")


def test_voice_turns_then_spoken_notice_over_http(settings):
    session = FakeSession(search=[slots_outcome()])
    brain = FakeBrain(BrainResult("Ya reviso las horas.", [SEARCH]), BrainResult("¿Cuál prefiere?"), follow_ups=["Hay dos horas: ¿cuál prefiere?"])
    services, _ = build(settings, agent=FakeClinicAgent(session), brain=brain)
    with TestClient(create_app(services)) as client:
        assert client.get("/api/speech/next").status_code == 204  # nada pendiente
        r = client.post("/api/process-audio", content=WAV, headers={"content-type": "audio/wav"})
        assert r.status_code == 200 and r.content == b"AUDIO" and r.headers["content-type"] == "audio/wav"
        assert unquote(r.headers["x-transcript"]) == "Quiero una hora con el cardiólogo" and unquote(r.headers["x-reply"]) == "Ya reviso las horas."
        wait_for_task(client, r.headers["x-task-ids"])

        notice = client.get("/api/speech/next")
        assert notice.status_code == 200 and notice.content == b"AUDIO"
        assert unquote(notice.headers["x-reply"]) == "Hay dos horas: ¿cuál prefiere?" and notice.headers["x-speech-id"]
        assert client.get("/api/speech/next").status_code == 204  # ya se entregó

        client.post("/api/process-audio", content=WAV)  # siguiente turno: Claude ve las horas encontradas en su contexto
    assert "2) viernes 2 de octubre, 10:30 - Dr. Pérez" in brain.seen[1][1]
    assert "Aún no tienes" not in brain.seen[1][1] or "Datos que aún no tienes: nombre completo, RUT, teléfono." in brain.seen[1][1]


def test_speech_notice_is_retried_after_a_synth_failure(settings):
    synth = FakeSynth()
    services, _ = build(settings, synth=synth)
    with TestClient(create_app(services)) as client:
        asyncio.run(services.speech.push("Hola"))
        synth.fail = True
        services.speech._items[0].audio = None  # como si la síntesis previa hubiera fallado
        assert client.get("/api/speech/next").status_code == 502  # no se pierde el aviso
        synth.fail = False
        again = client.get("/api/speech/next")
    assert again.status_code == 200 and again.content == b"AUDIO" and again.headers["content-type"] == "audio/wav"
    assert unquote(again.headers["x-reply"]) == "Hola"


def test_emergency_alert_is_dispatched_immediately(settings):
    services, posts = build(settings, brain=FakeBrain(BrainResult("Llame al 131.", [ALERT])))
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
        wait_for_task(client, r.headers["x-task-ids"])
    assert (posts[0]["category"], posts[0]["status"]) == ("alerta", "alerta") and posts[0]["metadata"]["event_type"] == "emergency"
    assert posts[0]["message"] == "Le duele el pecho." and "Emergencia" in posts[0]["title"] and posts[0]["elder"] == "Rosa"


def test_undeliverable_alert_marks_task_error(settings):
    services, _ = build(settings, brain=FakeBrain(BrainResult("ok", [ALERT])), panel_status=500)
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
        task = wait_for_task(client, r.headers["x-task-ids"])
    assert task["status"] == "error" and task["result"]["delivered"] is False


def test_safeguard_replaces_the_models_phrase_in_the_voice_reply(settings):
    brain = FakeBrain(BrainResult("Ya le estoy reservando la hora.", [BOOK]))
    services, posts = build(settings, brain=brain)
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
    assert r.headers["x-task-ids"] == "" and "tengo horas" in unquote(r.headers["x-reply"]).lower()
    assert services.synthesizer.spoken[0] == unquote(r.headers["x-reply"]) and posts == []


def test_empty_and_missing_audio_are_rejected(settings):
    services, _ = build(settings)
    with TestClient(create_app(services)) as client:
        assert client.post("/api/process-audio", content=b"").status_code == 422
        assert client.post("/api/process-audio").status_code == 422


def test_empty_transcript_asks_to_repeat_without_calling_claude(settings):
    brain = FakeBrain(BrainResult("no se usa"))
    services, posts = build(settings, transcriber=FakeTranscriber(""), brain=brain)
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
    assert r.status_code == 200 and r.headers["x-error"] == "empty_transcript"
    assert brain.seen == [] and posts == []
    assert "repetir" in unquote(r.headers["x-reply"])


@pytest.mark.parametrize("stage,kwargs", [
    ("stt", {"transcriber": FakeTranscriber(RuntimeError("groq caído"))}),
    ("llm", {"brain": FakeBrain(error=RuntimeError("api caída"))}),
])
def test_stage_failures_speak_an_apology_and_notify_the_panel(settings, stage, kwargs):
    services, posts = build(settings, **kwargs)
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
        time.sleep(0.2)
    assert r.status_code == 200 and r.headers["x-error"] == stage and r.content == b"AUDIO"
    assert posts and posts[0]["metadata"]["event_type"] == "system_error" and posts[0]["metadata"]["stage"] == stage


def test_tts_failure_returns_502_but_tasks_keep_running(settings):
    services, posts = build(settings, brain=FakeBrain(BrainResult("ok", [ALERT])), synth=FakeSynth(fail=True))
    with TestClient(create_app(services)) as client:
        assert client.post("/api/process-audio", content=WAV).status_code == 502
        time.sleep(0.3)
    assert posts and posts[0]["metadata"]["event_type"] == "emergency"


def test_task_endpoints_and_health(settings):
    services, _ = build(settings, brain=FakeBrain(BrainResult("ok", [ALERT])))
    with TestClient(create_app(services)) as client:
        r = client.post("/api/process-audio", content=WAV)
        task_id = r.headers["x-task-ids"]
        wait_for_task(client, task_id)
        assert [t["id"] for t in client.get("/api/tasks").json()] == [task_id]
        assert client.get("/api/tasks/nope").status_code == 404
        health = client.get("/health").json()
    assert health["status"] == "ok" and health["model"] == "claude-sonnet-5-5"
    assert "avisar_al_cuidador: listo" in services.tasks.recent_summary()
