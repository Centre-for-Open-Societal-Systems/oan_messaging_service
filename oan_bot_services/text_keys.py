"""Every text key Bot Services can emit (TG-02, spec §5.5).

Bot Services returns keys and parameters, never sentences. Code refers to keys only
through these constants; a contract test checks every one is in
``telegram/contracts/text-keys.yaml`` and that nothing else in the package looks like a key.
"""

from __future__ import annotations

from typing import Final

COMMON_INVALID_CHOICE: Final = "common.invalid_choice"
COMMON_UNAVAILABLE: Final = "common.unavailable"

UPDATES_ON: Final = "updates.on"
UPDATES_OFF: Final = "updates.off"

STOP_DONE: Final = "stop.done"

STAFF_LINKED: Final = "staff.linked"
STAFF_CODE_INVALID: Final = "staff.code_invalid"
STAFF_CODE_EXPIRED: Final = "staff.code_expired"
STAFF_LOCKED: Final = "staff.locked"
STAFF_MENU: Final = "staff.menu"

LINK_CODE_SENT: Final = "link.code_sent"
LINK_NOT_FOUND: Final = "link.not_found"
LINK_NOT_OWN_CONTACT: Final = "link.not_own_contact"
LINK_RATE_LIMITED: Final = "link.rate_limited"
LINK_CODE_INVALID: Final = "link.code_invalid"
LINK_CODE_EXPIRED: Final = "link.code_expired"
LINK_LOCKED: Final = "link.locked"
LINK_DONE: Final = "link.done"
LINK_REMOVED: Final = "link.removed"
LINK_NONE: Final = "link.none"
LINK_INVITE: Final = "link.invite"

#: ``staff.linked`` takes ``role_name``: the role's own text key, which the flow renders.
ROLE_KEYS: Final = {
    "development_agent": "role.development_agent",
    "supervisor": "role.supervisor",
    "executive": "role.executive",
}

ALL: Final = frozenset(
    {
        COMMON_INVALID_CHOICE, COMMON_UNAVAILABLE, UPDATES_ON, UPDATES_OFF, STOP_DONE,
        STAFF_LINKED, STAFF_CODE_INVALID, STAFF_CODE_EXPIRED, STAFF_LOCKED, STAFF_MENU,
        LINK_CODE_SENT, LINK_NOT_FOUND, LINK_NOT_OWN_CONTACT, LINK_RATE_LIMITED,
        LINK_CODE_INVALID, LINK_CODE_EXPIRED, LINK_LOCKED, LINK_DONE, LINK_REMOVED,
        LINK_NONE, LINK_INVITE, *ROLE_KEYS.values(),
    }
)

#: Parameters each key takes (must match text-keys.yaml ``params``).
PARAMS: Final = {
    LINK_CODE_SENT: ("masked_phone",),
    STAFF_LINKED: ("role_name",),
}


def check_key(key: str) -> str:
    """Guard for keyed messages queued for the notify flow."""
    if key not in ALL:
        raise ValueError("unknown text key")
    return key
