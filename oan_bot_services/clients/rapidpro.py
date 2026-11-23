"""RapidPro web API v2 (26.3 line): contacts, groups, flow starts, delete contact.

Token in ``Authorization: Token <token>`` (a Kubernetes Secret; never logged). Contacts are
addressed by UUID only; no URN, chat id or name is sent from here.
"""

from __future__ import annotations

import uuid
from typing import Any

from oan_bot_services.clients.http import ServiceClient, Unavailable, json_object


class RapidPro:
    def __init__(self, http: ServiceClient, notify_flow: str, staff_group: str = "staff") -> None:
        self.http = http
        self.notify_flow = notify_flow
        self.staff_group = staff_group

    async def set_fields(self, contact: uuid.UUID, fields: dict[str, str | None]) -> None:
        r = await self.http.request(
            "POST", "/api/v2/contacts.json", params={"uuid": str(contact)},
            json={"fields": fields}, ok=(200, 201),
        )
        if r.status_code not in (200, 201):
            raise Unavailable("rapidpro", f"http_{r.status_code}")

    async def add_to_group(self, contact: uuid.UUID, group: str | None = None) -> None:
        r = await self.http.request(
            "POST", "/api/v2/contact_actions.json",
            json={"contacts": [str(contact)], "action": "add", "group": group or self.staff_group},
            ok=(200, 204),
        )
        if r.status_code not in (200, 204):
            raise Unavailable("rapidpro", f"http_{r.status_code}")

    async def current_flow(self, contact: uuid.UUID) -> str | None | bool:
        """The flow the contact is in (its UUID), None when idle, False when unknown contact."""
        r = await self.http.request("GET", "/api/v2/contacts.json", params={"uuid": str(contact)})
        if r.status_code != 200:
            raise Unavailable("rapidpro", f"http_{r.status_code}")
        results = json_object(r, "rapidpro").get("results") or []
        if not results:
            return False
        flow = results[0].get("flow")
        return str(flow.get("uuid")) if isinstance(flow, dict) else None

    async def start_notify(
        self, contact: uuid.UUID, params: dict[str, Any], *, exclude_active: bool
    ) -> None:
        """Start the ``notify`` flow for one keyed message (§5.8)."""
        if not self.notify_flow:
            raise Unavailable("rapidpro", "notify_flow_not_configured")
        r = await self.http.request(
            "POST", "/api/v2/flow_starts.json",
            json={
                "flow": self.notify_flow,
                "contacts": [str(contact)],
                "params": params,
                "exclude_active": exclude_active,
            },
            ok=(200, 201),
        )
        if r.status_code not in (200, 201):
            raise Unavailable("rapidpro", f"http_{r.status_code}")

    async def delete_contact(self, contact: uuid.UUID) -> None:
        """Deletes the contact and its message history (§5.4 step 5). Unknown = done."""
        r = await self.http.request(
            "DELETE", "/api/v2/contacts.json", params={"uuid": str(contact)}, ok=(204,),
        )
        if r.status_code not in (204, 404):
            raise Unavailable("rapidpro", f"http_{r.status_code}")
