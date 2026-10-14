from __future__ import annotations

import random
import uuid
from datetime import datetime

ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
TR_BANK_CODE_DIGITS = 5
TR_ACCOUNT_DIGITS = 16


def tckn_check(first9: str) -> str:
    digits = [int(c) for c in first9]
    odd = sum(digits[0:9:2])
    even = sum(digits[1:8:2])
    tenth = (odd * 7 - even) % 10
    eleventh = (sum(digits) + tenth) % 10
    return f"{tenth}{eleventh}"


def valid_tckn(value: str) -> bool:
    return (len(value) == 11 and value.isdigit() and value[0] != "0"
            and value[9:] == tckn_check(value[:9]))


def vkn_check(first9: str) -> str:
    total = 0
    for index, char in enumerate(first9):
        shifted = (int(char) + 9 - index) % 10
        weighted = (shifted * 2 ** (9 - index)) % 9
        if shifted != 0 and weighted == 0:
            weighted = 9
        total += weighted
    return str((10 - total % 10) % 10)


def valid_vkn(value: str) -> bool:
    return len(value) == 10 and value.isdigit() and value[9] == vkn_check(value[:9])


def _iban_number(text: str) -> int:
    return int("".join(str(int(char, 36)) for char in text))


def iban_check(country: str, bban: str) -> str:
    return f"{98 - _iban_number(bban + country + '00') % 97:02d}"


def valid_iban(value: str) -> bool:
    compact = value.replace(" ", "")
    return len(compact) >= 15 and _iban_number(compact[4:] + compact[:4]) % 97 == 1


class Ids:
    def __init__(self, seed: int, now: datetime) -> None:
        self._random = random.Random(seed)
        self._now = now
        self._count = 0

    def _digits(self, count: int) -> str:
        return "".join(str(self._random.randrange(10)) for _ in range(count))

    def tckn(self) -> str:
        first = str(self._random.randrange(1, 10)) + self._digits(8)
        return first + tckn_check(first)

    def vkn(self) -> str:
        first = self._digits(9)
        return first + vkn_check(first)

    def iban(self, bank_code: str = "00205") -> str:
        if len(bank_code) != TR_BANK_CODE_DIGITS or not bank_code.isdigit():
            raise ValueError(f"a Turkish bank code has {TR_BANK_CODE_DIGITS} digits, got {bank_code!r}")
        bban = bank_code + "0" + self._digits(TR_ACCOUNT_DIGITS)
        return "TR" + iban_check("TR", bban) + bban

    def ulid(self) -> str:
        millis = int(self._now.timestamp() * 1000) + self._count
        self._count += 1
        value = (millis << 80) | self._random.getrandbits(80)
        return "".join(ULID_ALPHABET[(value >> (5 * index)) & 31] for index in reversed(range(26)))

    def uuid(self) -> str:
        return str(uuid.UUID(int=self._random.getrandbits(128), version=4))

    def phone(self) -> str:
        return "+905" + str(self._random.randrange(30, 60)) + self._digits(7)

    def email(self, local: str | None = None, domain: str = "example.test") -> str:
        self._count += 1
        name = local or f"user{self._count}-{self._random.getrandbits(24):06x}"
        return f"{name}@{domain}"


__all__ = ["Ids", "iban_check", "tckn_check", "valid_iban", "valid_tckn", "valid_vkn", "vkn_check"]
