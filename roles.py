"""roles.py — список ролей (из ТЗ) и его разбор. Без зависимостей от окружения."""

import re

from roles_data import ROLES_RAW


def _parse_roles(raw: str) -> list[dict]:
    roles = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        gender_m = re.search(r"\{\s*(м|ж)\s*\}", line)
        gender = gender_m.group(1) if gender_m else "м"
        head = re.split(r"\s\(|\s\{", line, maxsplit=1)[0]
        names = [n.strip() for n in head.split(",") if n.strip()]
        surname = ""
        if "(" in line:
            surname = line[line.index("(") + 1: line.rindex(")")] if ")" in line else line[line.index("(") + 1:]
            surname = re.sub(r"\{.*?\}", "", surname).strip()
        roles.append({"names": names, "gender": gender, "surname": surname})
    return roles


ROLES: list[dict] = _parse_roles(ROLES_RAW)
ROLE_KEYS: list[str] = [r["names"][0] for r in ROLES]


def get_role_gender(role_key: str) -> str:
    for r in ROLES:
        if r["names"][0] == role_key:
            return r["gender"]
    return "м"
