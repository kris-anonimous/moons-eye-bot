"""
services/pure.py — «чистые» функции (без aiogram, MongoDB и сети).

Вынесены отдельно, чтобы их можно было тестировать без запуска бота.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from roles import ROLES

MSK_OFFSET = dt.timedelta(hours=3)

# ---------------------------------------------------------------------------
# Время
# ---------------------------------------------------------------------------


def to_msk(t: dt.datetime) -> dt.datetime:
    return t + MSK_OFFSET


def fmt_msk(t: Optional[dt.datetime], with_time: bool = True) -> str:
    if t is None:
        return "—"
    m = to_msk(t)
    return m.strftime("%Y-%m-%d %H:%M") if with_time else m.strftime("%d.%m")


def next_midnight_msk(now_utc: dt.datetime, days: int) -> dt.datetime:
    """Полночь по МСК через `days` календарных дней (результат — naive UTC)."""
    msk_date = to_msk(now_utc).date() + dt.timedelta(days=days)
    return dt.datetime(msk_date.year, msk_date.month, msk_date.day) - MSK_OFFSET


def msk_date_str(t: dt.datetime) -> str:
    return to_msk(t).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Роли
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _surname_tokens(surname_raw: str) -> set[str]:
    cleaned = re.sub(r"[()/]", " ", surname_raw)
    return set(normalize(cleaned).split())


def match_roles_local(text: str) -> list[str]:
    """Вернуть канонические роли, найденные в тексте (без ИИ).

    Фамилия одного персонажа, совпадающая с именем другого (Кимимару Кагуя),
    не считается второй ролью.
    """
    norm = " " + normalize(text) + " "
    found: list[int] = []
    for idx, role in enumerate(ROLES):
        for name in role["names"]:
            n = normalize(name)
            if n and f" {n} " in norm:
                found.append(idx)
                break
    if len(found) > 1:
        drop = set()
        for a in found:
            tokens = _surname_tokens(ROLES[a]["surname"])
            for b in found:
                if a != b and any(normalize(n) in tokens for n in ROLES[b]["names"]):
                    drop.add(b)
        found = [i for i in found if i not in drop]
    return [ROLES[i]["names"][0] for i in found]


# ---------------------------------------------------------------------------
# Даты и длительности
# ---------------------------------------------------------------------------


def parse_dd_mm(text: str, year: int) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\s*", text or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        dt.date(year, month, day)
    except ValueError:
        return None
    return day, month


def format_dd_mm(day: int, month: int) -> str:
    return f"{day:02d}.{month:02d}"


_UNITS = [
    (r"мин", 60),
    (r"час|ч\b", 3600),
    (r"дн|ден|дне|сут", 86400),
    (r"нед", 7 * 86400),
    (r"мес", 30 * 86400),
]


def parse_duration(text: str) -> Optional[dt.timedelta]:
    """«2 часа», «30 минут», «неделя», «3 дня», «1 месяц»."""
    t = (text or "").lower()
    m = re.search(r"(\d+)?\s*(мин\w*|час\w*|ч\b|дн\w*|ден\w*|сут\w*|нед\w*|мес\w*)", t)
    if not m:
        return None
    num = int(m.group(1)) if m.group(1) else 1
    unit = m.group(2)
    for pat, sec in _UNITS:
        if re.match(pat, unit):
            return dt.timedelta(seconds=num * sec)
    return None


def human_duration(td: dt.timedelta) -> str:
    total = int(td.total_seconds())
    if total % 86400 == 0 and total >= 86400:
        n = total // 86400
        return f"{n} {'день' if n == 1 else 'дня' if n < 5 else 'дней'}"
    if total % 3600 == 0 and total >= 3600:
        n = total // 3600
        return f"{n} {'час' if n == 1 else 'часа' if n < 5 else 'часов'}"
    n = max(total // 60, 1)
    return f"{n} {'минуту' if n == 1 else 'минуты' if n < 5 else 'минут'}"


def human_wait(td: dt.timedelta) -> str:
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    return f"{h}ч {rem // 60}мин"


# ---------------------------------------------------------------------------
# Теги (лимит Telegram — 16 символов, эмодзи нельзя)
# ---------------------------------------------------------------------------


def build_tag_text(role: str, resting: bool = False, limit: int = 16) -> str:
    if not resting:
        return role[:limit]
    full = f"{role}|рест"
    if len(full) <= limit:
        return full
    short = f"{role}|Р"
    return short if len(short) <= limit else short[:limit]


# ---------------------------------------------------------------------------
# Разбор форм
# ---------------------------------------------------------------------------


def parse_leave_note(text: str, role_key: str) -> Optional[str]:
    """Форма выхода: «Роль: X / Причина: Y» либо «X / Y». Вернуть причину."""
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    role_in: Optional[str] = None
    reason: Optional[str] = None
    m1 = re.match(r"(?i)^роль\s*[:\-–—]\s*(.+)$", lines[0])
    m2 = re.match(r"(?i)^причина\s*[:\-–—]\s*(.+)$", lines[1])
    if m1 and m2:
        role_in, reason = m1.group(1), " ".join([m2.group(1)] + lines[2:])
    else:
        role_in, reason = lines[0], " ".join(lines[1:])
    if normalize(role_in) != normalize(role_key):
        return None
    return reason.strip() or None


def parse_punishment_message(text: str) -> Optional[tuple[str, str]]:
    m = re.search(
        r"нарушение\s+по\s+(\d+\.\d+)\s*[\n,;.]*\s*наказание\s*:\s*(.+)",
        text or "", flags=re.I | re.S,
    )
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def penalty_from_text(text: str) -> Optional[tuple[str, Optional[int]]]:
    """Текст наказания -> (код, секунды для мута)."""
    t = (text or "").lower().strip()
    count_m = re.search(r"(\d)\s*шт", t)
    count = int(count_m.group(1)) if count_m else 1
    count = min(max(count, 1), 3)
    if "вечный бан" in t or "вечный" in t:
        return "perm", None
    if t.startswith("мут"):
        d = parse_duration(t[3:]) or dt.timedelta(hours=1)
        return "mute", int(d.total_seconds())
    if "устный" in t:
        return {1: "oral", 2: "oral2", 3: "oral3"}[count], None
    if "варн" in t:
        return {1: "warn", 2: "warn2", 3: "warn3"}[count], None
    if t.startswith("бан"):
        return "ban", None
    return None


def parse_poll_form(text: str) -> Optional[dict]:
    m = re.search(
        r"заголовок\s*:\s*(.+?)\s*,?\s*варианты\s+ответов\s*:\s*(.+?)\s*\.?\s*"
        r"викторина\s*:\s*(да|нет)\s*\.?\s*время\s+опроса\s*:\s*(.+?)"
        r"(?:\s*\.?\s*правильный\s+ответ\s*:\s*(\d+))?\s*$",
        text or "", flags=re.I | re.S,
    )
    if not m:
        return None
    title, opts_raw, quiz, dur_raw, correct = m.groups()
    options = [
        o.strip().rstrip(".").strip()
        for o in re.findall(r"\d+\s*[.)]\s*(.+?)(?=\s+\d+\s*[.)]\s|\s*$)", opts_raw, flags=re.S)
    ]
    options = [o for o in options if o]
    dur = parse_duration(dur_raw)
    if len(options) < 2 or len(options) > 10 or dur is None:
        return None
    return {
        "title": title.strip(),
        "options": options,
        "is_quiz": quiz.lower() == "да",
        "duration": dur,
        "correct": (int(correct) - 1) if correct else 0,
    }


def parse_add_member(text: str) -> Optional[tuple[int, str, str]]:
    m = re.search(
        r"участник\s*:\s*(\d+)\s*,\s*роль\s*:\s*(.+?)\s*,\s*день\s+рождения\s*:\s*(\d{1,2}\.\d{1,2})",
        text or "", flags=re.I | re.S,
    )
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip(), m.group(3)


def parse_hhmm(text: str) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


# ---------------------------------------------------------------------------
# Оценка возраста аккаунта по id
# ---------------------------------------------------------------------------


def estimate_account_age_days(user_id: int, now: dt.datetime, anchors: list) -> Optional[int]:
    pts = sorted((i, dt.datetime.strptime(d, "%Y-%m-%d")) for i, d in anchors)
    if user_id <= pts[0][0]:
        return (now - pts[0][1]).days
    for (i1, d1), (i2, d2) in zip(pts, pts[1:]):
        if i1 <= user_id <= i2:
            frac = (user_id - i1) / (i2 - i1)
            created = d1 + (d2 - d1) * frac
            return max((now - created).days, 0)
    # id выше последней точки: считаем по темпу последнего интервала
    (i1, d1), (i2, d2) = pts[-2], pts[-1]
    rate = (d2 - d1).total_seconds() / (i2 - i1)
    created = d2 + dt.timedelta(seconds=rate * (user_id - i2))
    return max((now - created).days, 0)


def count_words(text: str) -> int:
    return len((text or "").split())


def gw(gender: Optional[str], male: str, female: str) -> str:
    return female if gender == "ж" else male
