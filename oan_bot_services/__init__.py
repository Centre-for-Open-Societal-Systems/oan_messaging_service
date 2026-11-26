"""OAN Bot Services: the routes RapidPro flows call (Channels Specification §5.8), the
Telegram copy of DA broadcasts sent through OAN Messaging (§6.8, SMS-02 revised), and one
worker for timers, broadcast sending and purging.

Phase A, wave 1. Bot Services returns text keys and parameters, never sentences (TG-02),
and logs only ``contact_ref`` for a farmer (§5.4).
"""
