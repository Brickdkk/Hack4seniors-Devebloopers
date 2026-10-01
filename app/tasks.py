"""Tareas en segundo plano: ejecuta las herramientas que pidió Claude, protege la reserva y despacha resultados.

Flujo de agendar (cada paso es un turno de la conversación):
  buscar_horas -> [aviso hablado con las horas] -> proponer_reserva -> [la persona dice sí] -> reservar_hora
Las salvaguardas viven aquí, no solo en el prompt: reservar_hora no se ejecuta si no hay una propuesta
vigente para esa misma hora; en ese caso se convierte en la pregunta de confirmación.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError

from .booking import BookingState, Pending, Slot
from .brain import ToolCall
from .browser_agent import AgentOutcome, ClinicAgent, ClinicRequest, ClinicSession
from .config import Settings
from .dispatcher import CaregiverDispatcher
from .schemas import CaregiverEvent, Severity, TaskInfo, now_iso

log = logging.getLogger("tasks")

MAX_TASKS_KEPT = 100
SESSION_TTL_S = 600  # el navegador de una reserva sin terminar se cierra a los 10 minutos
REAPER_EVERY_S = 30
URGENCY_TO_SEVERITY: dict[str, Severity] = {"info": "info", "importante": "important", "emergencia": "emergency"}
VISIBLE_KINDS = {"buscar_horas", "reservar_hora", "tramitar_en_clinica", "avisar_al_cuidador"}
ASK_DATA = "Para reservar necesito su nombre completo y su RUT. ¿Me los puede decir?"

Announcer = Callable[[str], Awaitable[None]]


def slot_text(slot: Slot) -> str:
    return f"opción {slot.option}: {slot.label()} (fecha {slot.fecha or '?'}, hora {slot.hora or '?'}, profesional {slot.profesional or '?'})"


def _portal_error(exc: Exception) -> AgentOutcome:
    first = (str(exc).strip().splitlines() or ["error"])[0][:160]
    return AgentOutcome(False, f"No se pudo abrir el portal de la clínica: {first}")


class TaskManager:
    def __init__(self, settings: Settings, dispatcher: CaregiverDispatcher, clinic_agent: ClinicAgent):
        self.settings = settings
        self.dispatcher = dispatcher
        self.clinic_agent = clinic_agent
        self.booking = BookingState({"nombre": settings.patient_full_name, "rut": settings.patient_rut, "telefono": settings.patient_phone, "edad": settings.patient_age})
        self.session: ClinicSession | None = None
        self.announcer: Announcer | None = None  # lo conecta el pipeline: convierte un aviso en voz
        self._tasks: OrderedDict[str, TaskInfo] = OrderedDict()
        self._running: set[asyncio.Task] = set()  # referencias fuertes: asyncio solo guarda débiles
        self._reapers: set[asyncio.Task] = set()
        self._reaper: asyncio.Task | None = None
        self._op_lock = asyncio.Lock()  # las operaciones sobre el portal de una reserva van de a una

    # -- consulta ---------------------------------------------------------------------------------

    def get(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def list(self) -> list[TaskInfo]:
        return list(reversed(self._tasks.values()))

    def recent_summary(self, limit: int = 5) -> str:
        recent = [t for t in self.list() if t.kind in VISIBLE_KINDS][:limit]
        if not recent:
            return "(ninguna)"
        estados = {"queued": "en cola", "running": "en curso", "success": "listo", "error": "con problemas"}
        return "\n".join(f"- {t.kind}: {estados[t.status]}. {t.summary}".strip() for t in recent)

    def context(self) -> str:
        """Estado que se le entrega a Claude en cada turno."""
        return f"{self.booking.context()}\n\nTareas recientes:\n{self.recent_summary()}"

    async def wait_all(self) -> None:
        while self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def aclose(self) -> None:
        await self.wait_all()
        for reaper in self._reapers:
            reaper.cancel()
        session, self.session = self.session, None
        if session:
            await session.close()

    # -- envío ------------------------------------------------------------------------------------

    def dispatch(self, calls: list[ToolCall], user_request: str, reply: str) -> tuple[list[str], str]:
        """Lanza las herramientas pedidas. Devuelve (ids de tareas, frase a decir): la frase cambia si una salvaguarda interviene."""
        ids: list[str] = []
        for call in calls:
            override = self._screen(call)
            if override is not None:
                log.info("Salvaguarda sobre %s: %s", call.name, override)
                reply = override
                continue
            ids.append(self.submit(call, user_request).id)
        return ids, reply

    def _screen(self, call: ToolCall) -> str | None:
        """Devuelve una frase si la llamada no puede seguir tal cual (se dice esa frase en lugar de la del modelo)."""
        if call.name not in ("proponer_reserva", "reservar_hora"):
            return None
        st, a = self.booking, call.input
        if st.phase == "booking":
            return "Ya estoy reservando esa hora, un momentito más."
        slot = st.slot(a.get("opcion"))
        if slot is None:
            return "Disculpe, no encuentro esa hora entre las que revisé. ¿Cuál prefiere?" if st.slots else "Todavía no tengo horas para ofrecerle. ¿Quiere que las busque?"

        if call.name == "proponer_reserva":
            has_name = a.get("nombre_completo", "").strip() or st.patient.get("nombre")
            has_rut = a.get("rut", "").strip() or st.patient.get("rut")
            return None if (has_name and has_rut) else ASK_DATA

        pending = st.valid_pending()
        if pending is not None and pending.option == slot.option:
            return None
        # Sin confirmación previa para esa hora: no se reserva, se pregunta.
        if st.patient.get("nombre") and st.patient.get("rut"):
            st.pending = Pending(slot.option)
            return f"Antes de reservar, ¿me confirma que quiere la hora {slot.label()}, a nombre de {st.patient['nombre']}?"
        return ASK_DATA

    def submit(self, call: ToolCall, user_request: str) -> TaskInfo:
        task = TaskInfo(id=uuid.uuid4().hex[:12], kind=call.name, created_at=now_iso(self.settings.timezone))
        self._tasks[task.id] = task
        while len(self._tasks) > MAX_TASKS_KEPT:
            self._tasks.popitem(last=False)
        st, a = self.booking, call.input

        # Herramientas que solo cambian el estado: se resuelven al instante, antes del siguiente turno.
        if call.name == "proponer_reserva":
            st.update_patient(a.get("nombre_completo", ""), a.get("rut", ""), a.get("telefono", ""))
            st.pending = Pending(a["opcion"])
            self._finish(task, "success", "Esperando la confirmación de la persona.")
            return task
        if call.name == "abandonar_reserva":
            st.reset()
            self._retire_session()
            self._finish(task, "success", "Reserva en curso cancelada.")
            return task

        # Herramientas en segundo plano: el estado se fija ya (para que el turno siguiente lo vea) y el trabajo corre aparte.
        if call.name == "buscar_horas":
            prev = st.phase
            if not (prev == "need_info" and self.session is not None):
                st.request = ClinicRequest("agendar_hora", a.get("especialidad", ""), a.get("fecha_preferida", ""), a.get("horario_preferido", ""), a.get("detalles", ""), user_request)
                st.slots, st.pending = [], None
            st.phase, st.need = "searching", ""
            runner = functools.partial(self._run_search, prev_phase=prev)
        elif call.name == "reservar_hora":
            pending = st.valid_pending()
            slot = st.slot(pending.option) if pending else None
            if slot is None:  # _screen ya lo impide; por si alguien llama submit directo
                self._finish(task, "error", "No hay una reserva confirmada que ejecutar.")
                return task
            st.pending, st.phase, st.need = None, "booking", ""
            runner = functools.partial(self._run_book, slot=slot, extra=a.get("datos_extra", "").strip())
        elif call.name == "tramitar_en_clinica":
            runner = self._run_clinic
        elif call.name == "avisar_al_cuidador":
            runner = self._run_alert
        else:
            self._finish(task, "error", f"Herramienta desconocida: {call.name}")
            return task
        self._spawn(self._guarded(runner, task, call, user_request), track=True)
        return task

    # -- infraestructura --------------------------------------------------------------------------

    def _spawn(self, coro, track: bool = False) -> None:
        bg = asyncio.create_task(coro)
        pool = self._running if track else self._reapers
        pool.add(bg)
        bg.add_done_callback(pool.discard)

    async def _guarded(self, runner, task: TaskInfo, call: ToolCall, user_request: str) -> None:
        try:
            task.status = "running"
            await runner(task, call, user_request)
        except Exception as exc:  # una tarea que falla nunca debe tumbar el servidor
            log.exception("Falló la tarea %s", task.id)
            self._finish(task, "error", f"Error inesperado: {type(exc).__name__}")
            await self.dispatcher.send(self._event(task, "system_error", "error", "important", task.summary, user_request, {"tool": call.name}))
            if call.name in ("buscar_horas", "reservar_hora"):
                self.booking.reset()
                self._retire_session()
                await self._announce("Hubo un problema técnico con el portal de la clínica y no se pudo avanzar. Díselo con calma y ofrécele intentarlo de nuevo.")

    def _finish(self, task: TaskInfo, status: str, summary: str, result: dict | None = None) -> None:
        task.status = status  # type: ignore[assignment]
        task.summary = summary
        task.result.update(result or {})
        task.finished_at = now_iso(self.settings.timezone)

    def _event(self, task: TaskInfo, event_type, status, severity, summary: str, user_request: str, details: dict) -> CaregiverEvent:
        return CaregiverEvent(
            event_id=task.id,
            event_type=event_type,
            status=status,
            severity=severity,
            timestamp=now_iso(self.settings.timezone),
            elder_name=self.settings.elder_name,
            summary=summary,
            user_request=user_request,
            details=details,
        )

    def report_system_error(self, summary: str, user_request: str = "", details: dict | None = None) -> None:
        """Avisa al panel de una falla del sistema sin hacer esperar la respuesta de voz."""
        event = CaregiverEvent(
            event_id=uuid.uuid4().hex[:12],
            event_type="system_error",
            status="error",
            severity="important",
            timestamp=now_iso(self.settings.timezone),
            elder_name=self.settings.elder_name,
            summary=summary,
            user_request=user_request,
            details=details or {},
        )
        self._spawn(self.dispatcher.send(event), track=True)

    async def _announce(self, notice: str) -> None:
        """Le pasa a Claude el resultado de una tarea para que se lo diga a la persona por voz."""
        if self.announcer is None:
            return
        try:
            await self.announcer(notice)
        except Exception:
            log.exception("No se pudo avisar por voz: %s", notice)

    # -- sesión del portal ------------------------------------------------------------------------

    async def _live_session(self) -> ClinicSession | None:
        s = self.session
        if s is None:
            return None
        if s.is_open and s.idle_s < SESSION_TTL_S:
            return s
        self._retire_session()
        return None

    async def _open_session(self, run_id: str) -> ClinicSession:
        session = self.clinic_agent.new_session(run_id)
        self.session = session
        try:
            await session.open()
        except BaseException:
            self.session = None
            raise
        self._reaper = asyncio.create_task(self._reap(session))
        self._reapers.add(self._reaper)
        self._reaper.add_done_callback(self._reapers.discard)
        return session

    def _retire_session(self, linger: bool = False) -> None:
        session, self.session = self.session, None
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        if session is not None:
            self._spawn(session.close(linger=linger), track=True)

    async def _reap(self, session: ClinicSession) -> None:
        """Cierra la ventana si la reserva se abandona (nadie vuelve a hablar)."""
        while self.session is session:
            await asyncio.sleep(REAPER_EVERY_S)
            if self.session is session and session.idle_s >= SESSION_TTL_S:
                log.info("Cerrando la sesión del portal por inactividad")
                self.booking.reset()
                self._retire_session()

    # -- herramientas -----------------------------------------------------------------------------

    async def _run_search(self, task: TaskInfo, call: ToolCall, user_request: str, prev_phase: str) -> None:
        st, a = self.booking, call.input
        async with self._op_lock:
            info = a.get("datos_paciente", "").strip()
            try:
                session = await self._live_session()
                if prev_phase == "need_info" and session is not None and info:
                    outcome = await session.provide_info(info)
                else:
                    request = st.request
                    if info:
                        request.detalles = f"{request.detalles} {info}".strip()
                    continuing = session is not None
                    if session is None:
                        session = await self._open_session(task.id)
                    outcome = await session.search(request, st.patient, continuing)
            except PlaywrightError as exc:
                outcome = _portal_error(exc)

        if outcome.kind == "slots":
            st.slots = [Slot(i, d["descripcion"], d.get("fecha", ""), d.get("hora", ""), d.get("profesional", "")) for i, d in enumerate(outcome.slots, 1)]
            st.phase, st.need, st.pending = "slots", "", None
            if st.slots:
                notice = f"La búsqueda terminó: hay {len(st.slots)} horas disponibles (están en el estado actual con su número). {outcome.summary}"
            else:
                notice = f"La búsqueda terminó y no hay horas disponibles para lo pedido. {outcome.summary} Ofrécele buscar otro día."
            self._finish(task, "success", f"{len(st.slots)} horas encontradas", {"slots": [s.as_dict() for s in st.slots], "screenshot": outcome.screenshot})
        elif outcome.kind == "need_info":
            st.phase, st.need = "need_info", outcome.question
            notice = f"El portal necesita un dato de la persona para seguir: {outcome.question}"
            self._finish(task, "success", f"El portal pide un dato: {outcome.question}")
        else:
            st.reset()
            self._retire_session()
            self._finish(task, "error", outcome.summary)
            await self.dispatcher.send(
                self._event(task, "clinic_task", "error", "important", f"No se pudieron buscar horas: {outcome.summary}", user_request, {"task": "agendar_hora", "stage": "search"})
            )
            notice = f"No se pudieron buscar horas: {outcome.summary} Díselo con calma y ofrécele intentarlo de nuevo."
        await self._announce(notice)

    async def _run_book(self, task: TaskInfo, call: ToolCall, user_request: str, slot: Slot, extra: str) -> None:
        st = self.booking
        patient = dict(st.patient)
        async with self._op_lock:
            try:
                session = await self._live_session()
                from_scratch = session is None
                if session is None:
                    session = await self._open_session(task.id)
                outcome = await session.book(slot_text(slot), patient, extra, st.request, from_scratch)
            except PlaywrightError as exc:
                outcome = _portal_error(exc)

        if outcome.kind == "need_info":  # el portal quiere un dato más: se vuelve a la confirmación ya dada
            st.phase, st.need, st.pending = "slots", outcome.question, Pending(slot.option)
            self._finish(task, "success", f"El portal pide un dato más: {outcome.question}")
            await self._announce(f"Para terminar la reserva el portal pide además: {outcome.question} Pídeselo; cuando lo diga, llama reservar_hora con ese dato en datos_extra.")
            return

        details = {
            "task": "agendar_hora",
            "specialty": getattr(st.request, "especialidad", ""),
            "appointment": slot.as_dict(),
            "patient": {"name": patient.get("nombre", ""), "rut": patient.get("rut", ""), "phone": patient.get("telefono", "")},
            "confirmation": outcome.confirmation,
            "steps": outcome.steps,
            "final_url": outcome.final_url,
            "screenshot": outcome.screenshot,
        }
        ok = outcome.success
        event = self._event(task, "clinic_task", "success" if ok else "error", "info" if ok else "important", outcome.summary, user_request, details)
        event.redact = [v for v in patient.values() if v]  # nombre, RUT y teléfono no salen hacia el panel
        delivered = await self.dispatcher.send(event)
        self._finish(task, "success" if ok else "error", outcome.summary, {**details, "delivered": delivered})
        self._retire_session(linger=ok)
        st.reset()
        if ok:
            await self._announce(f"La hora quedó reservada. {outcome.summary} Comprobante: {outcome.confirmation or 'sin código'}. Confírmaselo con calma y dile que su cuidadora también quedó avisada.")
        else:
            await self._announce(f"No se pudo reservar la hora: {outcome.summary} Díselo con calma y ofrécele buscar otra hora.")

    async def _run_clinic(self, task: TaskInfo, call: ToolCall, user_request: str) -> None:
        a = call.input
        request = ClinicRequest(tramite=a.get("tramite", ""), detalles=a.get("detalles", ""), user_request=user_request)
        outcome = await self.clinic_agent.run(request, task.id, dict(self.booking.patient))

        if outcome.kind == "need_info":
            self._finish(task, "success", f"El portal pide un dato: {outcome.question}")
            await self._announce(f"El portal necesita un dato de la persona para seguir: {outcome.question} Pídeselo y, cuando lo diga, vuelve a llamar tramitar_en_clinica con ese dato en detalles.")
            return

        details = {"task": request.tramite, "confirmation": outcome.confirmation, "steps": outcome.steps, "final_url": outcome.final_url, "screenshot": outcome.screenshot}
        status = "success" if outcome.success else "error"
        event = self._event(task, "clinic_task", status, "info" if outcome.success else "important", outcome.summary, user_request, details)
        event.redact = [v for v in self.booking.patient.values() if v]
        delivered = await self.dispatcher.send(event)
        self._finish(task, status, outcome.summary, {**details, "delivered": delivered})
        await self._announce(f"Resultado de {request.tramite}: {'listo' if outcome.success else 'no se pudo'}. {outcome.summary} {outcome.confirmation}".strip())

    async def _run_alert(self, task: TaskInfo, call: ToolCall, user_request: str) -> None:
        urgency = call.input.get("urgencia", "info")
        message = call.input.get("mensaje", "").strip() or user_request
        event_type = "emergency" if urgency == "emergencia" else "caregiver_alert"
        event = self._event(task, event_type, "info", URGENCY_TO_SEVERITY.get(urgency, "info"), message, user_request, {"urgency": urgency})
        delivered = await self.dispatcher.send(event)
        if delivered:
            self._finish(task, "success", message, {"delivered": True})
        else:
            self._finish(task, "error", f"Aviso no entregado al panel (guardado en el outbox): {message}", {"delivered": False})
