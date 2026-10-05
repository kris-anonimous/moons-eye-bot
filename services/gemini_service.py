"""
services/gemini_service.py — обёртка над Google Gemini API (бесплатный тариф).

Что делает ИИ:
  * match_role()    — сопоставляет введённый текст с ролью из списка, когда
                      простой локальный разбор не справился (транслит, опечатки).
  * analyze_text()  — классифицирует сообщение по правилам, требующим понимания
                      смысла (0.x, 1.x, 2.1, 3.1–3.3). Наказание назначает БОТ по
                      таблице правил, а не Gemini.
  * analyze_media() — оценивает картинку (NSFW / крайний контент) для 3.0 и 3.5.

Список ролей остаётся источником истины: всё, что вернул Gemini, проверяется
по списку. Бесплатный тариф ограничен по частоте, поэтому есть свой лимитер;
при превышении анализ сообщений просто пропускается (а не копится очередью).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

import config
from roles import ROLES, ROLE_KEYS

logger = logging.getLogger(__name__)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_calls: deque = deque()
_dead_models: set[str] = set()


async def _acquire(wait_seconds: float = 0.0) -> bool:
    """Скользящее окно «не более N запросов в минуту»."""
    deadline = time.monotonic() + wait_seconds
    while True:
        now = time.monotonic()
        while _calls and now - _calls[0] > 60:
            _calls.popleft()
        if len(_calls) < config.GEMINI_MAX_CALLS_PER_MINUTE:
            _calls.append(now)
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1)


async def _call(parts: list[dict], wait_seconds: float = 0.0) -> Optional[str]:
    """Отправить запрос; при 404 (модель отключена) пробуем запасные модели."""
    if not await _acquire(wait_seconds):
        return None
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    models = [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]
    async with aiohttp.ClientSession() as session:
        for model in models:
            if model in _dead_models:
                continue
            try:
                async with session.post(
                    _BASE.format(model=model), json=payload,
                    headers={"x-goog-api-key": config.GEMINI_API_KEY},
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as resp:
                    if resp.status == 404:
                        logger.error("Модель Gemini %s недоступна (404) — пробую следующую. "
                                     "Обновите GEMINI_MODEL.", model)
                        _dead_models.add(model)
                        continue
                    if resp.status != 200:
                        logger.warning("Gemini %s: статус %s: %s", model, resp.status, (await resp.text())[:300])
                        return None
                    data = await resp.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        return None
                    out = cands[0].get("content", {}).get("parts", [])
                    return out[0].get("text") if out else None
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка запроса к Gemini")
                return None
    return None


def _loads(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Роли
# ---------------------------------------------------------------------------

async def match_role(user_text: str) -> Optional[str]:
    """Вернуть каноническую роль, "MULTIPLE", None (нет совпадений) или "ERROR"."""
    lines = []
    for r in ROLES:
        extra = f" (фамилия: {r['surname']})" if r["surname"] else ""
        lines.append(f'- {r["names"][0]}: варианты имени: {", ".join(r["names"])}{extra}')
    prompt = f"""Ты сопоставляешь текст пользователя с ролью (персонаж вселенной Naruto) из СТРОГОГО списка.
Пользователь мог написать имя с фамилией или без, фамилию первой или второй, с опечатками,
латиницей (транслит) или другим вариантом чтения.

Список ролей:
{chr(10).join(lines)}

Текст пользователя: "{user_text}"

Правила ответа:
- Ровно одна роль из списка -> верни её каноническое имя (первое имя в строке роли).
- Несколько разных ролей одновременно -> верни "MULTIPLE".
- Ни одна роль не подходит -> верни null.
Фамилия персонажа (например «Кагуя» у Кимимару) не является отдельной ролью, если рядом стоит имя.
Ответь JSON: {{"result": "<каноническое имя | MULTIPLE | null>"}}"""
    raw = await _call([{"text": prompt}], wait_seconds=8)
    data = _loads(raw)
    if data is None:
        return "ERROR"
    res = data.get("result")
    if res == "MULTIPLE":
        return "MULTIPLE"
    return res if res in ROLE_KEYS else None


# ---------------------------------------------------------------------------
# Модерация
# ---------------------------------------------------------------------------

async def analyze_text(text: str, context: list[str]) -> Optional[dict]:
    """Вернуть {"rule": "1.5", "reason": "..."} или None."""
    rules = "\n".join(f'{n}: {config.MODERATION_RULES[n]["text"]}' for n in config.AI_TEXT_RULES)
    ctx = "\n".join(f"- {c}" for c in context[-8:]) or "(нет)"
    prompt = f"""Ты модератор русскоязычного чата по ролевой игре во вселенной Naruto.
Участники часто играют роли: сцены драки, угроз, брани МЕЖДУ ПЕРСОНАЖАМИ внутри игры — это НЕ нарушение.
Нарушением считается только то, что происходит «вне игры», между реальными людьми, в нешуточном порядке.

Проверь ПОСЛЕДНЕЕ сообщение только на эти правила:
{rules}

Пояснения:
- 0.0: публикация чужих персональных данных (ФИО, возраст, адрес, телефон) без согласия.
- 0.5: нарушение — если человек пишет ТОЛЬКО на другом языке без русских вставок; фразы на другом языке
  в шутку, как пример или цитата — не нарушение.
- 1.3: реклама своего проекта/канала/чата, вредоносные ссылки.
- 2.1: очень длинный бессмысленный текст/паста (история, обращённая к собеседнику — не нарушение).
Будь консервативен: сомнительные случаи не отмечай.

Предыдущие сообщения автора:
{ctx}

Последнее сообщение: "{text[:1500]}"

Ответь JSON: {{"rule": "<номер правила или null>", "reason": "<одно предложение>"}}"""
    data = _loads(await _call([{"text": prompt}]))
    if not data:
        return None
    rule = data.get("rule")
    if rule in config.AI_TEXT_RULES:
        return {"rule": rule, "reason": data.get("reason", "")}
    return None


async def analyze_media(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[dict]:
    """Оценка картинки: {"nsfw": bool, "extreme": bool}."""
    prompt = """Оцени изображение для модерации чата (нужна только классификация).
Верни JSON: {"nsfw": true/false, "extreme": true/false}.
nsfw = обнажённое тело, порнографический или откровенно сексуальный контент.
extreme = сексуализация детей, жёсткая порнография, символы нацизма/фашизма, расчленёнка/жестокое насилие."""
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
    ]
    data = _loads(await _call(parts))
    if not data:
        return None
    return {"nsfw": bool(data.get("nsfw")), "extreme": bool(data.get("extreme"))}
