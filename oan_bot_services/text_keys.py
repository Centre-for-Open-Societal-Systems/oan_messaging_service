"""Every text key Bot Services can emit (TG-02, spec §5.5).

Bot Services returns keys and parameters, never sentences. Code refers to keys only
through these constants; a contract test checks every one is in
``telegram/contracts/text-keys.yaml`` and that nothing else in the package looks like a key.
"""

from __future__ import annotations

from typing import Final

COMMON_INVALID_CHOICE: Final = "common.invalid_choice"
COMMON_UNAVAILABLE: Final = "common.unavailable"
COMMON_SESSION_EXPIRED: Final = "common.session_expired"
COMMON_NOT_READ: Final = "common.not_read"

ONBOARDING_CHOOSE_LANGUAGE: Final = "onboarding.choose_language"
ONBOARDING_PRIVACY_NOTICE: Final = "onboarding.privacy_notice"
MENU_MAIN: Final = "menu.main"

UPDATES_ON: Final = "updates.on"
UPDATES_OFF: Final = "updates.off"

STOP_DONE: Final = "stop.done"
STOP_UPDATES_STOPPED: Final = "stop.updates_stopped"
STOP_STAFF: Final = "stop.staff"

STAFF_LINKED: Final = "staff.linked"
STAFF_CODE_INVALID: Final = "staff.code_invalid"
STAFF_CODE_EXPIRED: Final = "staff.code_expired"
STAFF_LOCKED: Final = "staff.locked"
STAFF_MENU: Final = "staff.menu"
STAFF_ASK_CODE: Final = "staff.ask_code"

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
LINK_ASK_FARMER_ID: Final = "link.ask_farmer_id"
LINK_ASK_CONTACT: Final = "link.ask_contact"

#: Reply keyboard buttons (each at most 30 UTF-16 units, checked when the file loads).
BTN_LANG_EN: Final = "btn.lang_en"
BTN_LANG_AM: Final = "btn.lang_am"
BTN_LINK_FARMER_ID: Final = "btn.link_farmer_id"
BTN_STAFF: Final = "btn.staff"
BTN_UPDATES_TOGGLE: Final = "btn.updates_toggle"
BTN_STOP_UPDATES: Final = "btn.stop_updates"
BTN_SHARE_PHONE: Final = "btn.share_phone"
BTN_MAIN_MENU: Final = "btn.main_menu"
BUTTONS: Final = frozenset(
    {
        BTN_LANG_EN, BTN_LANG_AM, BTN_LINK_FARMER_ID, BTN_STAFF, BTN_UPDATES_TOGGLE,
        BTN_STOP_UPDATES, BTN_SHARE_PHONE, BTN_MAIN_MENU,
    }
)

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
        COMMON_SESSION_EXPIRED, COMMON_NOT_READ, ONBOARDING_CHOOSE_LANGUAGE,
        ONBOARDING_PRIVACY_NOTICE, MENU_MAIN, STOP_UPDATES_STOPPED, STOP_STAFF,
        STAFF_ASK_CODE, LINK_ASK_FARMER_ID, LINK_ASK_CONTACT, *BUTTONS,
    }
)

#: Parameters each key takes (must match text-keys.yaml ``params``).
PARAMS: Final = {
    LINK_CODE_SENT: ("masked_phone",),
    STAFF_LINKED: ("role_name",),
    ONBOARDING_PRIVACY_NOTICE: ("retention_days", "log_days"),
    STOP_UPDATES_STOPPED: ("retention_days",),
}


def check_key(key: str) -> str:
    """Guard for keyed messages queued for the notify flow."""
    if key not in ALL:
        raise ValueError("unknown text key")
    return key
