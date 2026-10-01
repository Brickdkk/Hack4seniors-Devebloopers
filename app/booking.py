"""Estado de la reserva en curso (una conversación con una persona a la vez).

Sirve para dos cosas: darle contexto a Claude en cada turno (qué horas se encontraron, qué falta, qué espera
confirmación) y comprobar en el servidor que nunca se reserve sin una propuesta confirmada.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

PENDING_TTL_S = 300  # una propuesta sin confirmar vence a los 5 minutos
PATIENT_FIELDS = {"nombre": "nombre completo", "rut": "RUT", "telefono": "teléfono", "edad": "edad"}
REQUIRED_FIELDS = ("nombre", "rut")  # el teléfono solo se pide si el portal lo exige


@dataclass
class Slot:
    option: int
    descripcion: str
    fecha: str = ""
    hora: str = ""
    profesional: str = ""

    def label(self) -> str:
        return self.descripcion or " ".join(p for p in (self.fecha, self.hora, self.profesional) if p) or f"opción {self.option}"

    def as_dict(self) -> dict[str, Any]:
        return {"option": self.option, "description": self.descripcion, "date": self.fecha, "time": self.hora, "professional": self.profesional}


@dataclass
class Pending:
    """Propuesta de reserva que espera el "sí" de la persona."""

    option: int
    since: float = field(default_factory=time.monotonic)


class BookingState:
    def __init__(self, known_patient: dict[str, str] | None = None):
        self.known = {k: v.strip() for k, v in (known_patient or {}).items() if v and v.strip()}
        self.reset()

    def reset(self) -> None:
        """Termina la reserva en curso. Los datos de la persona se conservan para no volver a pedirlos."""
        self.request: Any = None  # ClinicRequest de la búsqueda
        self.slots: list[Slot] = []
        self.phase = "idle"  # idle | searching | need_info | slots | booking
        self.pending: Pending | None = None
        self.need = ""
        self.patient: dict[str, str] = {**self.known, **getattr(self, "patient", {})}

    # -- consulta ---------------------------------------------------------------------------------

    def slot(self, option: Any) -> Slot | None:
        return next((s for s in self.slots if s.option == option), None)

    def valid_pending(self) -> Pending | None:
        if self.pending and time.monotonic() - self.pending.since > PENDING_TTL_S:
            self.pending = None
        return self.pending

    def missing_patient_fields(self) -> list[str]:
        return [PATIENT_FIELDS[key] for key in REQUIRED_FIELDS if not self.patient.get(key)]

    def has_saved_profile(self) -> bool:
        return all(self.known.get(key) for key in REQUIRED_FIELDS)

    def update_patient(self, nombre: str = "", rut: str = "", telefono: str = "") -> None:
        for key, value in (("nombre", nombre), ("rut", rut), ("telefono", telefono)):  # la edad solo viene de los datos guardados
            if value and value.strip():
                self.patient[key] = value.strip()

    def context(self) -> str:
        """Texto que se le entrega a Claude en cada turno."""
        lines: list[str] = []
        r = self.request
        if self.phase == "idle":
            lines.append("No hay una reserva en curso.")
        else:
            lines.append(
                f"Reserva en curso: especialidad {getattr(r, 'especialidad', '') or '(sin definir)'}, "
                f"fecha preferida {getattr(r, 'fecha_preferida', '') or '(sin definir)'}, "
                f"horario {getattr(r, 'horario_preferido', '') or '(sin definir)'}."
            )
        if self.phase == "searching":
            lines.append("El sistema está buscando horas en el portal; todavía no hay resultados. Si la persona pregunta, dile que ya casi.")
        elif self.phase == "slots":
            if self.slots:
                lines.append("Horas disponibles encontradas (usa estos números en `opcion`):")
                lines += [f"  {s.option}) {s.label()}" for s in self.slots]
            else:
                lines.append("El portal no mostró horas disponibles para lo pedido.")
        elif self.phase == "booking":
            lines.append("El sistema está reservando la hora elegida; todavía no hay confirmación.")
        if self.need:
            lines.append(f"El portal necesita este dato de la persona: {self.need}")

        if self.patient:
            known = ", ".join(f"{PATIENT_FIELDS[k]}: {v}{' años' if k == 'edad' else ''}" for k, v in self.patient.items() if k in PATIENT_FIELDS and v)
            lines.append(f"Datos de la persona que ya tienes: {known}.")
        if self.has_saved_profile():
            lines.append("Esos son sus datos GUARDADOS en el sistema: no se los pidas; al reservar, dile que rellenas todo con sus datos guardados.")
        missing = self.missing_patient_fields()
        if missing:
            lines.append(f"Datos obligatorios que aún no tienes: {', '.join(missing)}.")

        pending = self.valid_pending()
        if pending:
            slot = self.slot(pending.option)
            lines.append(f"Esperando que la persona confirme con un sí: opción {pending.option} ({slot.label() if slot else '?'}).")
        return "\n".join(lines)
