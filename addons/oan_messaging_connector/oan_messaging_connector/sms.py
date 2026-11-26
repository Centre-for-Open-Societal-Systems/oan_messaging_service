"""Rules OAN enforces: SMS length (an OTP in one part, any other text in at most
MAX_PARTS parts) and an Ethiopian mobile recipient (09 or 07 series)."""

import re

GSM7_BASIC = frozenset(
	"@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
	"¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")


#: OAN's limit (contract 3.1.0, SMS-09): a text other than an OTP may take this many parts.
MAX_PARTS = 3


def fits_one_sms(text: str) -> bool:
	"""Same rule OAN enforces for an OTP: 160 GSM-7 septets (extension chars = 2), else 70 UTF-16."""
	return sms_parts(text) == 1


def sms_parts(text: str) -> int:
	"""How many SMS parts OAN counts for ``text`` (oan_messaging.contracts.sms_parts).

	One part holds 160 GSM-7 septets, or 70 UTF-16 units when any character is outside
	GSM-7 (any Amharic letter). A longer text is concatenated at 153 septets or 67 units
	per part, and a two-unit character (GSM-7 extension, emoji) is never split.
	"""
	if all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in text):
		sizes = [2 if c in GSM7_EXTENSION else 1 for c in text]
		single, per_part = 160, 153
	else:
		sizes = [len(c.encode("utf-16-le")) // 2 for c in text]
		single, per_part = 70, 67
	if sum(sizes) <= single:
		return 1
	parts, room = 1, per_part
	for size in sizes:
		if size > room:
			parts, room = parts + 1, per_part
		room -= size
	return parts


#: The recipient forms OAN accepts (its ``ET_MOBILE_INPUT_PATTERN``). ``[0-9]``, not ``\d``:
#: in Python ``\d`` also matches non-ASCII digits.
ET_MOBILE = re.compile(r"^(\+?251[79][0-9]{8}|0[79][0-9]{8})$")
_SEPARATORS = re.compile(r"[\s\-().]")


def clean_mobile(raw: str | None) -> str | None:
	"""``raw`` without spaces, dashes, dots or brackets if it is an Ethiopian mobile OAN
	accepts (``09…``, ``07…``, ``2519…``, ``+2519…``, ``2517…``, ``+2517…``), else None."""
	candidate = _SEPARATORS.sub("", raw or "")
	return candidate if ET_MOBILE.fullmatch(candidate) else None
