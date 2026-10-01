"""Despacho de eventos al Panel del Cuidador (HTTP POST) con respaldo local en data/outbox.jsonl."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from .config import Settings
from .panel import panel_endpoint, to_panel_log
from .schemas import CaregiverEvent

log = logging.getLogger("dispatcher")

ATTEMPTS = 3
BACKOFF_S = 0.5


class CaregiverDispatcher:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._outbox = settings.data_dir / "outbox.jsonl"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, event: CaregiverEvent) -> bool:
        """Envía el evento al panel (en su esquema de logs). Siempre queda en el outbox; devuelve True si el panel lo recibió."""
        payload = to_panel_log(event, self.settings.caregiver_name, self.settings.timezone)
        delivered, error, panel_id = await self._post(payload, event.event_id)
        self._append_outbox(event, payload, delivered, error, panel_id)
        tag = f"ENTREGADO (log {panel_id})" if delivered else f"NO ENTREGADO ({error})"
        print(f"[panel] {payload['category']}/{payload['status']} {tag}: {payload['title']}", flush=True)
        return delivered

    async def _post(self, payload: dict, event_id: str) -> tuple[bool, str, int | None]:
        if not self.settings.caregiver_panel_url:
            return False, "CAREGIVER_PANEL_URL vacío", None
        url = panel_endpoint(self.settings.caregiver_panel_url)
        error = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = await self._client.post(url, json=payload)
                if resp.is_success:
                    try:
                        panel_id = resp.json().get("id")
                    except ValueError:
                        panel_id = None
                    return True, "", panel_id
                error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # error del cliente: reintentar no sirve
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}" or type(exc).__name__
            if attempt < ATTEMPTS:
                await asyncio.sleep(BACKOFF_S * attempt)
        log.warning("No se pudo entregar el evento %s a %s: %s", event_id, url, error)
        return False, error, None

    def _append_outbox(self, event: CaregiverEvent, payload: dict, delivered: bool, error: str, panel_id: int | None) -> None:
        try:
            self._outbox.parent.mkdir(parents=True, exist_ok=True)
            line = {"delivered": delivered, "error": error, "panel_log_id": panel_id, "event": event.model_dump(mode="json"), "panel_payload": payload}
            with self._outbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("No se pudo escribir el outbox")
