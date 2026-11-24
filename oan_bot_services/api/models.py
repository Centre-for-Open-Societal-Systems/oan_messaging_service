"""Request bodies, exactly as ``telegram/contracts/bot-services.openapi.yaml`` and the DA
kit's ``bot-services-broadcasts.openapi.yaml`` define them (unknown fields refused)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints

AreaCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,16}$")]
Language = Literal["am", "en"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Contact(_Strict):
    uuid: uuid.UUID
    telegram_id: StrictInt


class LocationIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    kebele_code: AreaCode
    language: Language


class UpdatesIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    updates: Literal["on", "off"]


class IdleIn(_Strict):
    contact: Contact


class StopIn(_Strict):
    request_id: uuid.UUID
    contact: Contact


class StaffLinkIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{8}$")]


class StaffInboundIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    text: Annotated[str, Field(max_length=4096)] | None = None
    attachments: Annotated[list[str], Field(max_length=10)] | None = None


class SharedContact(_Strict):
    user_id: StrictInt
    phone_number: Annotated[str, Field(max_length=20)]


class FarmerLinkStartIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    farmer_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{4,32}$")]
    shared_contact: SharedContact


class FarmerLinkVerifyIn(_Strict):
    request_id: uuid.UUID
    contact: Contact
    link_request_id: uuid.UUID
    code: Annotated[str, StringConstraints(pattern=r"^[0-9]{4,8}$")]


class UnlinkIn(_Strict):
    request_id: uuid.UUID
    contact: Contact


class Scope(_Strict):
    level: Literal["region", "zone", "woreda", "kebele"]
    code: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class BroadcastIn(_Strict):
    recipients: Literal["farmers", "das"]
    scope: Scope
    sender_ref: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    sender_role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    source: Literal["Knowledge", "Emergency", "Weather", "Informational"]
    source_reference: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
    title: Annotated[str, Field(max_length=140)]
    body: str
    expires_at: AwareDatetime | None = None
