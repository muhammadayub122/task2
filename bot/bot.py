from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib import error, request
from uuid import UUID

from django.apps import apps
from apps.utils import validate_card, card_mask, display_card
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model
from django.db.models.fields import (
    BigAutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    FloatField,
    IntegerField,
    PositiveBigIntegerField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
    SmallAutoField,
    SmallIntegerField,
    TextField,
    UUIDField,
)
from django.utils.dateparse import parse_date, parse_datetime

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "ru"
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
SKIPPED_VALUE = object()
SUPPORTED_LANGUAGES = {"ru", "en", "uz"}
SKIP_TOKENS = {
    "/skip",
    "skip",
    "-",
    "пропуск",
    "пропустить",
    "otkazib",
    "o'tkazib",
    "otkazib yuborish",
    "o'tkazib yuborish",
}
NUMBER_RANGE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*(?:\.\.|:|to|-)\s*(-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
INTEGER_RANGE_RE = re.compile(
    r"^\s*(-?\d+)\s*(?:\.\.|:|to|-)\s*(-?\d+)\s*$",
    re.IGNORECASE,
)
DATE_RANGE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s*(?:\.\.|:|to|-)\s*(\d{4}-\d{2}-\d{2})\s*$",
    re.IGNORECASE,
)
DATETIME_RANGE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*"
    r"(?:\.\.|:|to)\s*"
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*$",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"^\+?\d{9,15}$")
CARD_NUMBER_RE = re.compile(r"^\d{12,19}$")
EXPIRY_RE = re.compile(r"^\s*(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})\s*$")
UZ_PHONE_RE = re.compile(r"^(33|50|55|77|88|90|91|93|94|95|97|98|99)\d{7}$")


class TelegramBotError(Exception):
    pass


class ValidationProblem(TelegramBotError):
    pass


@dataclass
class ParsedFilter:
    lookups: dict[str, Any]
    display_value: str


@dataclass
class FieldSpec:
    field_name: str
    label: str
    field: Any
    required: bool = False
    semantic: str = "generic"


@dataclass
class ChatSession:
    language: str = DEFAULT_LANGUAGE
    mode: str = "idle"
    current_index: int = 0
    filters: dict[str, ParsedFilter] = field(default_factory=dict)
    card_payload: dict[str, Any] = field(default_factory=dict)
    transfer_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotConfig:
    token: str
    allowed_chat_ids: set[int]
    result_limit: int
    poll_timeout: int
    retry_delay: int
    history_file: Path
    default_language: str


def bootstrap_django() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if not os.getenv("DJANGO_SETTINGS_MODULE"):
        os.environ["DJANGO_SETTINGS_MODULE"] = _discover_settings_module(project_root)

    import django

    django.setup()


def _discover_settings_module(project_root: Path) -> str:
    candidates: list[tuple[int, str]] = []
    ignored_parts = {
        ".git",
        ".venv",
        "__pycache__",
        "env",
        "migrations",
        "node_modules",
        "site-packages",
        "venv",
    }

    for settings_file in project_root.rglob("settings.py"):
        if any(part in ignored_parts for part in settings_file.parts):
            continue

        relative_path = settings_file.relative_to(project_root).with_suffix("")
        module_name = ".".join(relative_path.parts)
        parent = settings_file.parent
        score = len(relative_path.parts)
        if (parent / "urls.py").exists():
            score -= 3
        if (parent / "wsgi.py").exists() or (parent / "asgi.py").exists():
            score -= 2
        candidates.append((score, module_name))

    if not candidates:
        raise RuntimeError("Unable to find a Django settings module automatically.")

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _get_setting(*names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(settings, name):
            value = getattr(settings, name)
            if value not in (None, ""):
                return value

        value = os.getenv(name)
        if value not in (None, ""):
            return value

    return default


def _parse_int_set(raw_value: Any) -> set[int]:
    if not raw_value:
        return set()

    if isinstance(raw_value, str):
        items = [item.strip() for item in raw_value.split(",")]
    else:
        items = list(raw_value)

    parsed: set[int] = set()
    for item in items:
        if item in (None, ""):
            continue
        parsed.add(int(item))
    return parsed


def _project_root() -> Path:
    base_dir = getattr(settings, "BASE_DIR", None)
    if base_dir:
        return Path(base_dir)
    return Path(__file__).resolve().parents[1]


def _build_config() -> BotConfig:
    token = _get_setting("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError(
            "Telegram token is missing. Set TELEGRAM_BOT_TOKEN in Django settings or env."
        )

    history_file = Path(
        _get_setting(
            "TELEGRAM_HISTORY_FILE",
            default=str(_project_root() / "telegram_bot_history.json"),
        )
    )
    default_language = str(_get_setting("TELEGRAM_DEFAULT_LANGUAGE", default=DEFAULT_LANGUAGE)).lower()
    if default_language not in SUPPORTED_LANGUAGES:
        default_language = DEFAULT_LANGUAGE

    return BotConfig(
        token=token,
        allowed_chat_ids=_parse_int_set(_get_setting("TELEGRAM_ALLOWED_CHAT_IDS", default="")),
        result_limit=max(1, int(_get_setting("TELEGRAM_RESULT_LIMIT", default=10))),
        poll_timeout=max(1, int(_get_setting("TELEGRAM_POLL_TIMEOUT", default=30))),
        retry_delay=max(1, int(_get_setting("TELEGRAM_RETRY_DELAY", default=3))),
        history_file=history_file,
        default_language=default_language,
    )


class TelegramApiClient:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        request_payload = json.dumps(payload or {}).encode("utf-8")
        telegram_request = request.Request(
            f"{self.base_url}/{method}",
            data=request_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(telegram_request) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise TelegramBotError(
                f"Telegram API error {exc.code} while calling {method}: {details}"
            ) from exc
        except error.URLError as exc:
            raise TelegramBotError(f"Network error while calling Telegram: {exc}") from exc

        if not response_data.get("ok"):
            raise TelegramBotError(
                f"Telegram API returned an error for {method}: {response_data}"
            )

        return response_data["result"]

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload)

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunk_text(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk})


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("History file is invalid JSON. Resetting it: %s", self.path)
            return []

    def append(self, entry: dict[str, Any]) -> None:
        items = self._read()
        items.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_for_chat(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        items = self._read()
        filtered = [item for item in items if item.get("chat_id") == int(chat_id)]
        return filtered[-limit:]


class DjangoTelegramBot:
    transfer_steps = ("from_card", "to_card", "amount", "comment", "otp")

    def __init__(self, config: BotConfig):
        self.config = config
        self.client = TelegramApiClient(config.token)
        self.sessions: dict[int, ChatSession] = {}
        self.history_store = HistoryStore(config.history_file)

        self.filter_model = self._resolve_filter_model()
        self.filter_fields = self._resolve_filter_fields(self.filter_model)
        self.result_fields = self._resolve_result_fields(self.filter_model)

        self.card_model = self._resolve_card_model()
        self.card_field_map = self._build_card_field_map()
        self.add_card_fields = self._resolve_add_card_fields()

    def run(self) -> None:
        logger.info(
            "Telegram bot started. Filter model: %s.%s",
            self.filter_model._meta.app_label,
            self.filter_model.__name__,
        )
        offset: int | None = None

        while True:
            try:
                for update in self.client.get_updates(offset, self.config.poll_timeout):
                    offset = update["update_id"] + 1
                    self._handle_update(update)
            except KeyboardInterrupt:
                logger.info("Telegram bot stopped by user.")
                raise
            except Exception:
                logger.exception("Telegram bot loop failed. Retrying soon.")
                time.sleep(self.config.retry_delay)

    def _session(self, chat_id: int) -> ChatSession:
        session = self.sessions.get(chat_id)
        if session is None:
            session = ChatSession(language=self.config.default_language)
            self.sessions[chat_id] = session
        return session

    def _reset_flow_state(self, session: ChatSession) -> None:
        session.mode = "idle"
        session.current_index = 0
        session.card_payload.clear()
        session.transfer_payload.clear()

    def _clear_filters(self, session: ChatSession) -> None:
        session.filters.clear()
        if session.mode == "search":
            session.current_index = 0

    def _text(
        self,
        chat_id: int,
        ru_text: str,
        en_text: str,
        uz_text: str | None = None,
    ) -> str:
        session = self._session(chat_id)
        if session.language == "en":
            return en_text
        if session.language == "uz":
            return uz_text or ru_text
        return ru_text

    def _validation_message(
        self,
        chat_id: int,
        field_label: str,
        ru_text: str,
        en_text: str,
        uz_text: str | None = None,
    ) -> str:
        return self._text(
            chat_id,
            f'Поле "{field_label}": {ru_text}',
            f'Field "{field_label}": {en_text}',
            f'"{field_label}" maydoni: {uz_text or ru_text}',
        )

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return
        chat_id = int(chat_id)

        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Этот чат не разрешен для использования бота.",
                    "This chat is not allowed for this bot.",
                ),
            )
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return

        session = self._session(chat_id)
        if session.mode == "search":
            self._handle_search_reply(chat_id, session, text)
            return
        if session.mode == "add_card":
            self._handle_add_card_reply(chat_id, session, text)
            return
        if session.mode == "share_money":
            self._handle_share_money_reply(chat_id, session, text)
            return

        self.client.send_message(chat_id, self._build_help_message(chat_id))

    def _handle_command(self, chat_id: int, text: str) -> None:
        session = self._session(chat_id)
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if command == "/start":
            self._reset_flow_state(session)
            self._clear_filters(session)
            self.client.send_message(chat_id, self._build_welcome_message(chat_id))
            self.client.send_message(chat_id, self._build_menu_message(chat_id))
            return

        if command in {"/lang", "/language"}:
            self._change_language(chat_id, session, arg)
            return

        if command == "/menu":
            self.client.send_message(chat_id, self._build_menu_message(chat_id))
            return

        if command == "/help":
            self.client.send_message(chat_id, self._build_help_message(chat_id))
            return

        if command == "/filters":
            self.client.send_message(chat_id, self._build_filters_message(chat_id))
            return

        if command == "/search":
            self._reset_flow_state(session)
            self._clear_filters(session)
            session.mode = "search"
            self.client.send_message(chat_id, self._build_search_intro(chat_id))
            self.client.send_message(chat_id, self._build_search_prompt(chat_id, session))
            return

        if command == "/show":
            self._send_search_results(chat_id, session)
            return

        if command == "/reset":
            self._reset_flow_state(session)
            self._clear_filters(session)
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Фильтры и текущие действия сброшены.",
                    "Filters and current actions were reset.",
                ),
            )
            return

        if command == "/cancel":
            self._reset_flow_state(session)
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Текущее действие отменено.",
                    "The current action was cancelled.",
                ),
            )
            return

        if command == "/skip":
            self._handle_skip(chat_id, session)
            return

        if command == "/addcard":
            self._start_add_card(chat_id, session)
            return

        if command == "/mycards":
            self._show_my_cards(chat_id)
            return

        if command == "/sharemoney":
            self._start_share_money(chat_id, session)
            return

        if command == "/history":
            self._show_history(chat_id)
            return

        self.client.send_message(chat_id, self._build_help_message(chat_id))

    def _change_language(self, chat_id: int, session: ChatSession, arg: str) -> None:
        if not arg:
            current = {
                "ru": "Русский",
                "en": "English",
                "uz": "O'zbekcha",
            }.get(session.language, "Русский")
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    f"Текущий язык: {current}\nИспользуйте /language ru, /language en или /language uz.",
                    f"Current language: {current}\nUse /language ru, /language en, or /language uz.",
                    f"Joriy til: {current}\n/language ru, /language en yoki /language uz dan foydalaning.",
                ),
            )
            return

        if arg not in SUPPORTED_LANGUAGES:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Поддерживаются только языки: ru, en, uz.",
                    "Only these languages are supported: ru, en, uz.",
                    "Faqat shu tillar qo'llab-quvvatlanadi: ru, en, uz.",
                ),
            )
            return

        session.language = arg
        if arg == "ru":
            self.client.send_message(chat_id, "Язык изменен на русский.")
        elif arg == "en":
            self.client.send_message(chat_id, "Language changed to English.")
        else:
            self.client.send_message(chat_id, "Til o'zbekchaga o'zgartirildi.")

    def _handle_skip(self, chat_id: int, session: ChatSession) -> None:
        if session.mode == "search":
            self._advance_search(chat_id, session)
            return

        if session.mode == "add_card":
            field_spec = self.add_card_fields[session.current_index]
            if field_spec.required:
                self.client.send_message(
                    chat_id,
                    self._text(
                        chat_id,
                        "Это обязательное поле. Его нельзя пропустить.",
                        "This field is required and cannot be skipped.",
                    ),
                )
                return
            self._advance_add_card(chat_id, session)
            return

        if session.mode == "share_money":
            step = self.transfer_steps[session.current_index]
            if step != "comment":
                self.client.send_message(
                    chat_id,
                    self._text(
                        chat_id,
                        "Этот шаг нельзя пропустить.",
                        "This step cannot be skipped.",
                    ),
                )
                return
            session.transfer_payload["comment"] = ""
            self._finish_share_money(chat_id, session)
            return

        self.client.send_message(
            chat_id,
            self._text(
                chat_id,
                "Сейчас нечего пропускать.",
                "There is nothing to skip right now.",
            ),
        )

    def _handle_search_reply(self, chat_id: int, session: ChatSession, text: str) -> None:
        field_spec = self.filter_fields[session.current_index]
        try:
            parsed_filter = self._parse_filter_value(chat_id, field_spec.field, text)
        except ValidationProblem as exc:
            self.client.send_message(
                chat_id,
                f"{exc}\n\n{self._build_search_prompt(chat_id, session)}",
            )
            return

        if parsed_filter is not None:
            session.filters[field_spec.field_name] = parsed_filter

        self._advance_search(chat_id, session)

    def _advance_search(self, chat_id: int, session: ChatSession) -> None:
        session.current_index += 1
        if session.current_index >= len(self.filter_fields):
            session.mode = "idle"
            session.current_index = 0
            self._send_search_results(chat_id, session)
            return

        self.client.send_message(chat_id, self._build_search_prompt(chat_id, session))

    def _send_search_results(self, chat_id: int, session: ChatSession) -> None:
        queryset = self.filter_model.objects.all()
        for parsed_filter in session.filters.values():
            queryset = queryset.filter(**parsed_filter.lookups)
        queryset = self._apply_default_ordering(queryset)

        total_count = queryset.count()
        results = list(queryset[: self.config.result_limit])

        if not total_count:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Ничего не найдено.\n\n"
                    f"{self._format_filter_summary(chat_id, session)}\n"
                    "Используйте /search, чтобы попробовать другие фильтры.",
                    "No results found.\n\n"
                    f"{self._format_filter_summary(chat_id, session)}\n"
                    "Use /search to try different filters.",
                ),
            )
            return

        lines = [
            self._text(
                chat_id,
                f"Найдено результатов: {total_count}.",
                f"Found {total_count} result(s).",
            ),
            self._format_filter_summary(chat_id, session),
            "",
        ]

        if total_count > len(results):
            lines.append(
                self._text(
                    chat_id,
                    f"Показываю первые {len(results)} результатов.",
                    f"Showing the first {len(results)} result(s).",
                )
            )
            lines.append("")

        for index, item in enumerate(results, start=1):
            lines.append(f"{index}. {self._format_instance(item)}")
            lines.append("")

        self.client.send_message(chat_id, "\n".join(lines).strip())

    def _start_add_card(self, chat_id: int, session: ChatSession) -> None:
        if self.card_model is None or not self.add_card_fields:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Добавление карты сейчас недоступно.",
                    "Adding cards is not available right now.",
                ),
            )
            return

        self._reset_flow_state(session)
        session.mode = "add_card"
        self.client.send_message(
            chat_id,
            self._text(
                chat_id,
                "Начинаем добавление новой карты. Отвечайте на вопросы по очереди.",
                "Starting new card creation. Please answer the questions step by step.",
                "Yangi karta qo'shishni boshlaymiz. Savollarga ketma-ket javob bering.",
            ),
        )
        self.client.send_message(chat_id, self._build_add_card_prompt(chat_id, session))

    def _handle_add_card_reply(self, chat_id: int, session: ChatSession, text: str) -> None:
        field_spec = self.add_card_fields[session.current_index]
        try:
            value, display_value = self._parse_card_field_value(chat_id, field_spec, text)
        except ValidationProblem as exc:
            self.client.send_message(
                chat_id,
                f"{exc}\n\n{self._build_add_card_prompt(chat_id, session)}",
            )
            return

        if value is not SKIPPED_VALUE:
            session.card_payload[field_spec.field_name] = value
            session.card_payload[f"{field_spec.field_name}__display"] = display_value
        self._advance_add_card(chat_id, session)

    def _advance_add_card(self, chat_id: int, session: ChatSession) -> None:
        session.current_index += 1
        if session.current_index >= len(self.add_card_fields):
            self._finish_add_card(chat_id, session)
            return

        self.client.send_message(chat_id, self._build_add_card_prompt(chat_id, session))

    def _finish_add_card(self, chat_id: int, session: ChatSession) -> None:
        try:
            payload = {
                key: value
                for key, value in session.card_payload.items()
                if not key.endswith("__display")
            }
            owner_field = self.card_field_map.get("owner")
            if owner_field is not None:
                payload[owner_field.name] = self._coerce_chat_id(owner_field, chat_id)

            card = self.card_model(**payload)
            card.full_clean()
            card.save()
        except ValidationError as exc:
            self._reset_flow_state(session)
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    f"Не удалось сохранить карту: {'; '.join(exc.messages)}\nИспользуйте /addcard, чтобы начать заново.",
                    f"Could not save the card: {'; '.join(exc.messages)}\nUse /addcard to start again.",
                ),
            )
            return

        self.history_store.append(
            {
                "type": "card_created",
                "chat_id": chat_id,
                "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
                "card_id": card.pk,
                "card_label": self._card_label(card),
            }
        )

        self._reset_flow_state(session)
        self.client.send_message(
            chat_id,
            self._text(
                chat_id,
                f"Карта успешно добавлена: {self._card_label(card)}",
                f"Card created successfully: {self._card_label(card)}",
            ),
        )

    def _start_share_money(self, chat_id: int, session: ChatSession) -> None:
        if self.card_model is None:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Модель карты не найдена.",
                    "Card model was not found.",
                ),
            )
            return

        if self.card_field_map.get("owner") is None or self.card_field_map.get("balance") is None:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Для перевода нужны поля владельца карты и баланса.",
                    "Money sharing requires card owner and balance fields.",
                ),
            )
            return

        user_cards = list(self._get_user_cards(chat_id))
        if not user_cards:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "У вас пока нет карт. Сначала добавьте карту через /addcard.",
                    "You do not have any cards yet. Add one first with /addcard.",
                ),
            )
            return

        self._reset_flow_state(session)
        session.mode = "share_money"
        self.client.send_message(
            chat_id,
            self._text(
                chat_id,
                "Начинаем перевод денег. Сначала выберите карту отправителя.",
                "Starting money transfer. First choose the source card.",
                "Pul o'tkazishni boshlaymiz. Avval yuboruvchi kartani tanlang.",
            ),
        )
        self.client.send_message(chat_id, self._build_share_money_prompt(chat_id, session))

    def _handle_share_money_reply(self, chat_id: int, session: ChatSession, text: str) -> None:
        step = self.transfer_steps[session.current_index]
        try:
            if step == "from_card":
                card = self._resolve_card_reference(chat_id, text, only_user_cards=True)
                session.transfer_payload["from_card"] = card
            elif step == "to_card":
                card = self._resolve_card_reference(chat_id, text, only_user_cards=False)
                if card.pk == session.transfer_payload["from_card"].pk:
                    raise ValidationProblem(
                        self._text(
                            chat_id,
                            "Карта получателя должна отличаться от карты отправителя.",
                            "Destination card must be different from the source card.",
                        )
                    )
                session.transfer_payload["to_card"] = card
            elif step == "amount":
                amount = self._parse_transfer_amount(chat_id, text)
                source_card = session.transfer_payload["from_card"]
                balance_field = self.card_field_map["balance"]
                source_balance = Decimal(str(getattr(source_card, balance_field.name)))
                if amount > source_balance:
                    raise ValidationProblem(
                        self._text(
                            chat_id,
                            "Недостаточно средств на карте отправителя.",
                            "Not enough money on the source card.",
                        )
                    )
                session.transfer_payload["amount"] = amount
            elif step == "comment":
                comment = "" if text.strip().lower() in SKIP_TOKENS else text.strip()
                if len(comment) > 255:
                    raise ValidationProblem(
                        self._text(
                            chat_id,
                            "Комментарий слишком длинный. Максимум 255 символов.",
                            "Comment is too long. Maximum is 255 characters.",
                        )
                    )
                session.transfer_payload["comment"] = comment

                # Initiate transfer through services
                from apps.services import create_transfer, BusinessError
                import uuid
                ext_id = f"tr-{uuid.uuid4()}"
                try:
                    create_transfer({
                        "ext_id": ext_id,
                        "sender_card_number": session.transfer_payload["from_card"].card_number,
                        "sender_card_expiry": session.transfer_payload["from_card"].expire.strftime("%m/%y"),
                        "receiver_card_number": session.transfer_payload["to_card"].card_number,
                        "sending_amount": session.transfer_payload["amount"],
                        "currency": 860,
                        "chat_id": chat_id
                    })
                    session.transfer_payload["ext_id"] = ext_id
                except BusinessError as exc:
                    raise ValidationProblem(str(exc))
            elif step == "otp":
                session.transfer_payload["otp"] = text.strip()
        except ValidationProblem as exc:
            self.client.send_message(
                chat_id,
                f"{exc}\n\n{self._build_share_money_prompt(chat_id, session)}",
            )
            return

        session.current_index += 1
        if session.current_index >= len(self.transfer_steps):
            self._finish_share_money(chat_id, session)
            return

        self.client.send_message(chat_id, self._build_share_money_prompt(chat_id, session))

    def _finish_share_money(self, chat_id: int, session: ChatSession) -> None:
        from apps.services import confirm_transfer, BusinessError
        from apps.models import Card
        
        ext_id = session.transfer_payload["ext_id"]
        otp = session.transfer_payload["otp"]
        
        try:
            confirm_transfer({"ext_id": ext_id, "otp": otp})
        except BusinessError as exc:
             self._reset_flow_state(session)
             self.client.send_message(
                 chat_id,
                 self._text(
                     chat_id,
                     f"Не удалось подтвердить перевод: {exc.message}\nИспользуйте /sharemoney снова.",
                     f"Could not confirm transfer: {exc.message}\nTry /sharemoney again.",
                 ),
             )
             return

        source_card = Card.objects.get(card_number=session.transfer_payload["from_card"].card_number)
        destination_card = Card.objects.get(card_number=session.transfer_payload["to_card"].card_number)
        amount = session.transfer_payload["amount"]
        
        self.history_store.append({
            "type": "transfer",
            "chat_id": chat_id,
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "from_card_id": source_card.pk,
            "to_card_id": destination_card.pk,
            "from_card_label": self._card_label(source_card),
            "to_card_label": self._card_label(destination_card),
            "amount": str(amount),
            "comment": session.transfer_payload.get("comment", ""),
        })
        
        self._reset_flow_state(session)
        self.client.send_message(
            chat_id,
            self._text(
                chat_id,
                "Перевод выполнен успешно.\n"
                f"Откуда: {self._card_label(source_card)}\n"
                f"Куда: {self._card_label(destination_card)}\n"
                f"Сумма: {amount}\n"
                f"Новый баланс отправителя: {source_card.balance}",
                "Transfer completed successfully.\n"
                f"From: {self._card_label(source_card)}\n"
                f"To: {self._card_label(destination_card)}\n"
                f"Amount: {amount}\n"
                f"New source balance: {source_card.balance}",
            ),
        )

    def _show_my_cards(self, chat_id: int) -> None:
        owner_field = self.card_field_map.get("owner")
        if self.card_model is None or owner_field is None:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "Функция моих карт недоступна. У модели нет поля telegram_chat_id/chat_id.",
                    "My cards is unavailable because the model has no telegram_chat_id/chat_id field.",
                ),
            )
            return

        cards = list(self._get_user_cards(chat_id))
        if not cards:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "У вас пока нет сохраненных карт.",
                    "You do not have any saved cards yet.",
                ),
            )
            return

        lines = [
            self._text(chat_id, "Ваши карты:", "Your cards:"),
            "",
        ]
        for index, card in enumerate(cards, start=1):
            lines.append(f"{index}. {self._format_card_details(card)}")
            lines.append("")

        self.client.send_message(chat_id, "\n".join(lines).strip())

    def _show_history(self, chat_id: int) -> None:
        records = self.history_store.list_for_chat(chat_id, limit=10)
        if not records:
            self.client.send_message(
                chat_id,
                self._text(
                    chat_id,
                    "История пока пуста.",
                    "History is empty for now.",
                ),
            )
            return

        lines = [self._text(chat_id, "Последние действия:", "Recent actions:"), ""]
        for index, item in enumerate(reversed(records), start=1):
            timestamp = item.get("timestamp", "")
            if item.get("type") == "card_created":
                lines.append(
                    self._text(
                        chat_id,
                        f"{index}. [{timestamp}] Добавлена карта: {item.get('card_label', item.get('card_id'))}",
                        f"{index}. [{timestamp}] Card added: {item.get('card_label', item.get('card_id'))}",
                    )
                )
            else:
                comment = item.get("comment") or self._text(chat_id, "без комментария", "no comment")
                lines.append(
                    self._text(
                        chat_id,
                        f"{index}. [{timestamp}] Перевод {item.get('amount')} с {item.get('from_card_label')} на {item.get('to_card_label')} ({comment})",
                        f"{index}. [{timestamp}] Transfer {item.get('amount')} from {item.get('from_card_label')} to {item.get('to_card_label')} ({comment})",
                    )
                )

        self.client.send_message(chat_id, "\n".join(lines))

    def _build_welcome_message(self, chat_id: int) -> str:
        return self._text(
            chat_id,
            f"Привет. Я бот для работы с {self.filter_model._meta.verbose_name_plural}.\n\n"
            "Русский язык включен по умолчанию. Полное меню ниже.",
            f"Welcome. I am a bot for working with {self.filter_model._meta.verbose_name_plural}.\n\n"
            "English is available. Full menu is below.",
            f"Salom. Men {self.filter_model._meta.verbose_name_plural} bilan ishlaydigan botman.\n\n"
            "O'zbek tili mavjud. To'liq menyu quyida.",
        )

    def _build_menu_message(self, chat_id: int) -> str:
        return self._text(
            chat_id,
            "Меню команд:\n"
            "/start - приветствие и меню\n"
            "/menu - показать все команды\n"
            "/search - пошаговый поиск по модели\n"
            "/show - показать результаты с текущими фильтрами\n"
            "/filters - показать доступные фильтры\n"
            "/addcard - добавить новую карту\n"
            "/mycards - список ваших карт\n"
            "/sharemoney - перевод денег между картами\n"
            "/history - последние действия\n"
            "/language ru - русский язык\n"
            "/language en - английский язык\n"
            "/language uz - узбекский язык\n"
            "/reset - сбросить фильтры и текущие действия\n"
            "/cancel - отменить текущее действие\n"
            "/skip - пропустить текущий шаг\n"
            "/help - подсказки и примеры",
            "Command menu:\n"
            "/start - welcome and menu\n"
            "/menu - show all commands\n"
            "/search - guided model search\n"
            "/show - show results using current filters\n"
            "/filters - show available filters\n"
            "/addcard - add a new card\n"
            "/mycards - list your cards\n"
            "/sharemoney - transfer money between cards\n"
            "/history - recent actions\n"
            "/language ru - Russian language\n"
            "/language en - English language\n"
            "/language uz - Uzbek language\n"
            "/reset - reset filters and current actions\n"
            "/cancel - cancel the current action\n"
            "/skip - skip the current step\n"
            "/help - tips and examples",
            "Buyruqlar menyusi:\n"
            "/start - salomlashuv va menyu\n"
            "/menu - barcha buyruqlarni ko'rsatish\n"
            "/search - model bo'yicha bosqichma-bosqich qidiruv\n"
            "/show - joriy filtrlar bo'yicha natijalarni ko'rsatish\n"
            "/filters - mavjud filtrlarni ko'rsatish\n"
            "/addcard - yangi karta qo'shish\n"
            "/mycards - mening kartalarim\n"
            "/sharemoney - kartalar orasida pul o'tkazish\n"
            "/history - oxirgi amallar tarixi\n"
            "/language ru - rus tili\n"
            "/language en - ingliz tili\n"
            "/language uz - o'zbek tili\n"
            "/reset - filtr va joriy amallarni tozalash\n"
            "/cancel - joriy amalni bekor qilish\n"
            "/skip - joriy bosqichni o'tkazib yuborish\n"
            "/help - yordam va misollar",
        )

    def _build_help_message(self, chat_id: int) -> str:
        return self._build_menu_message(chat_id) + "\n\n" + self._text(
            chat_id,
            "Подсказки:\n"
            "Текст: samsung\n"
            "Число: 100\n"
            "Диапазон: 100..500\n"
            "Дата: 2026-04-24\n"
            "Диапазон дат: 2026-01-01..2026-12-31\n"
            "Булево: да / нет\n"
            "Телефон: +998901234567 или 901234567\n"
            "Карта: только 16 цифр, обычно начинается с 8600 или 9860\n"
            "Срок карты: 12/28",
            "Tips:\n"
            "Text: samsung\n"
            "Number: 100\n"
            "Range: 100..500\n"
            "Date: 2026-04-24\n"
            "Date range: 2026-01-01..2026-12-31\n"
            "Boolean: yes / no\n"
            "Phone: +998901234567 or 901234567\n"
            "Card: 16 digits only, usually starts with 8600 or 9860\n"
            "Card expiry: 12/28",
            "Yordam:\n"
            "Matn: samsung\n"
            "Son: 100\n"
            "Oraliq: 100..500\n"
            "Sana: 2026-04-24\n"
            "Sana oralig'i: 2026-01-01..2026-12-31\n"
            "Mantiqiy qiymat: ha / yo'q\n"
            "Telefon: +998901234567 yoki 901234567\n"
            "Karta: faqat 16 ta raqam, odatda 8600 yoki 9860 bilan boshlanadi\n"
            "Karta muddati: 12/28",
        )

    def _build_filters_message(self, chat_id: int) -> str:
        lines = [
            self._text(
                chat_id,
                f"Доступные фильтры для {self.filter_model._meta.verbose_name}:",
                f"Available filters for {self.filter_model._meta.verbose_name}:",
            ),
            "",
        ]
        for field_spec in self.filter_fields:
            lines.append(f"- {field_spec.label}")
            lines.append(self._field_help_text(chat_id, field_spec.field))
        return "\n".join(lines)

    def _build_search_intro(self, chat_id: int) -> str:
        return self._text(
            chat_id,
            f"Начинаю поиск по {self.filter_model._meta.verbose_name_plural}. "
            "Отвечайте на вопросы по очереди или используйте /skip.",
            f"Starting a search for {self.filter_model._meta.verbose_name_plural}. "
            "Reply step by step or use /skip.",
            f"{self.filter_model._meta.verbose_name_plural} bo'yicha qidiruvni boshlayman. "
            "Savollarga ketma-ket javob bering yoki /skip dan foydalaning.",
        )

    def _build_search_prompt(self, chat_id: int, session: ChatSession) -> str:
        field_spec = self.filter_fields[session.current_index]
        return (
            f"[{session.current_index + 1}/{len(self.filter_fields)}] {field_spec.label}\n"
            f"{self._field_help_text(chat_id, field_spec.field)}"
        )

    def _build_add_card_prompt(self, chat_id: int, session: ChatSession) -> str:
        field_spec = self.add_card_fields[session.current_index]
        optional_note = "" if field_spec.required else self._text(
            chat_id,
            "\nМожно пропустить: /skip",
            "\nOptional: /skip",
        )
        return (
            f"[{session.current_index + 1}/{len(self.add_card_fields)}] {field_spec.label}\n"
            f"{self._card_field_help_text(chat_id, field_spec)}{optional_note}"
        )

    def _build_share_money_prompt(self, chat_id: int, session: ChatSession) -> str:
        step = self.transfer_steps[session.current_index]
        if step == "from_card":
            cards_text = self._list_user_card_choices(chat_id)
            return self._text(
                chat_id,
                "Выберите карту отправителя. Введите ID карты, полный номер или последние 4 цифры.\n\n"
                f"{cards_text}",
                "Choose the source card. Enter the card ID, full number, or last 4 digits.\n\n"
                f"{cards_text}",
                "Yuboruvchi kartani tanlang. Karta ID sini, to'liq raqamini yoki oxirgi 4 raqamini kiriting.\n\n"
                f"{cards_text}",
            )
        if step == "to_card":
            return self._text(
                chat_id,
                "Введите карту получателя: ID, полный номер или последние 4 цифры.",
                "Enter the destination card: ID, full number, or last 4 digits.",
                "Qabul qiluvchi kartani kiriting: ID, to'liq raqam yoki oxirgi 4 raqam.",
            )
        if step == "amount":
            return self._text(
                chat_id,
                "Введите сумму перевода. Например: 100 или 250.50",
                "Enter the transfer amount. Example: 100 or 250.50",
                "O'tkazma summasini kiriting. Masalan: 100 yoki 250.50",
            )
        return self._text(
            chat_id,
            "Введите комментарий к переводу или /skip, чтобы пропустить.",
            "Enter a comment for the transfer or /skip to skip it.",
            "O'tkazmaga izoh kiriting yoki /skip yuborib o'tkazib yuboring.",
        )

    def _format_filter_summary(self, chat_id: int, session: ChatSession) -> str:
        if not session.filters:
            return self._text(chat_id, "Текущие фильтры: нет", "Current filters: none")

        lines = [self._text(chat_id, "Текущие фильтры:", "Current filters:")]
        for field_spec in self.filter_fields:
            parsed_filter = session.filters.get(field_spec.field_name)
            if parsed_filter is None:
                continue
            lines.append(f"- {field_spec.label}: {parsed_filter.display_value}")
        return "\n".join(lines)

    def _field_help_text(self, chat_id: int, field: Any) -> str:
        if getattr(field, "choices", None):
            choices_preview = ", ".join(str(label) for _, label in list(field.choices)[:6])
            return self._text(
                chat_id,
                f"Выберите одно из значений: {choices_preview}. Или отправьте /skip.",
                f"Choose one of: {choices_preview}. Or send /skip.",
                f"Quyidagi qiymatlardan birini tanlang: {choices_preview}. Yoki /skip yuboring.",
            )
        if isinstance(field, BooleanField):
            return self._text(
                chat_id,
                "Введите да или нет.",
                "Enter yes or no.",
                "Ha yoki yo'q deb yozing.",
            )
        if isinstance(
            field,
            (
                IntegerField,
                PositiveIntegerField,
                PositiveSmallIntegerField,
                PositiveBigIntegerField,
                SmallIntegerField,
                BigIntegerField,
                BigAutoField,
                SmallAutoField,
            ),
        ):
            return self._text(
                chat_id,
                "Введите целое число или диапазон 10..20.",
                "Enter a whole number or a range like 10..20.",
                "Butun son yoki 10..20 kabi oraliq kiriting.",
            )
        if isinstance(field, (FloatField, DecimalField)):
            return self._text(
                chat_id,
                "Введите число или диапазон 10.5..20.5.",
                "Enter a number or a range like 10.5..20.5.",
                "Son yoki 10.5..20.5 kabi oraliq kiriting.",
            )
        if isinstance(field, DateTimeField):
            return self._text(
                chat_id,
                "Введите YYYY-MM-DD HH:MM, YYYY-MM-DD или диапазон.",
                "Enter YYYY-MM-DD HH:MM, YYYY-MM-DD, or a range.",
                "YYYY-MM-DD HH:MM, YYYY-MM-DD yoki oraliq kiriting.",
            )
        if isinstance(field, DateField):
            return self._text(
                chat_id,
                "Введите YYYY-MM-DD или диапазон дат.",
                "Enter YYYY-MM-DD or a date range.",
                "YYYY-MM-DD yoki sana oralig'ini kiriting.",
            )
        if isinstance(field, UUIDField):
            return self._text(
                chat_id,
                "Введите полный UUID.",
                "Enter the full UUID.",
                "To'liq UUID ni kiriting.",
            )
        return self._text(
            chat_id,
            "Введите текст для поиска по части строки.",
            "Enter text to search by substring.",
            "Qatorning bir qismi bo'yicha qidirish uchun matn kiriting.",
        )

    def _card_field_help_text(self, chat_id: int, field_spec: FieldSpec) -> str:
        if field_spec.semantic == "card_number":
            return self._text(
                chat_id,
                "Введите номер карты. Ровно 16 цифр, обычно начинается с 8600 или 9860.",
                "Enter the card number. Exactly 16 digits, usually starting with 8600 or 9860.",
                "Karta raqamini kiriting. Aynan 16 ta raqam, odatda 8600 yoki 9860 bilan boshlanadi.",
            )
        if field_spec.semantic == "phone":
            return self._text(
                chat_id,
                "Введите телефон в формате +998901234567 или 901234567.",
                "Enter a phone number like +998901234567 or 901234567.",
                "Telefon raqamini +998901234567 yoki 901234567 formatida kiriting.",
            )
        if field_spec.semantic == "expire":
            return self._text(
                chat_id,
                "Введите срок действия карты. Например: 12/28 или 2028-12-31.",
                "Enter the card expiry. Example: 12/28 or 2028-12-31.",
                "Karta amal qilish muddatini kiriting. Masalan: 12/28 yoki 2028-12-31.",
            )
        if field_spec.semantic == "balance":
            return self._text(
                chat_id,
                "Введите стартовый баланс. Например: 0, 100 или 250.50.",
                "Enter the starting balance. Example: 0, 100 or 250.50.",
                "Boshlang'ich balansni kiriting. Masalan: 0, 100 yoki 250.50.",
            )
        return self._field_help_text(chat_id, field_spec.field)

    def _format_instance(self, instance: Model) -> str:
        rendered_fields: list[str] = []
        for field in self.result_fields:
            display_getter = getattr(instance, f"get_{field.name}_display", None)
            if callable(display_getter):
                value = display_getter()
            else:
                value = getattr(instance, field.name, None)
            formatted = self._format_scalar(value)
            if formatted:
                rendered_fields.append(f"{field.verbose_name}: {formatted}")
        return " | ".join(rendered_fields) if rendered_fields else str(instance)

    def _format_card_details(self, card: Model) -> str:
        lines = [f"ID: {card.pk}"]

        number_field = self.card_field_map.get("number")
        balance_field = self.card_field_map.get("balance")
        expire_field = self.card_field_map.get("expire")
        phone_field = self.card_field_map.get("phone")

        if number_field is not None:
            lines.append(f"{number_field.verbose_name}: {card_mask(str(getattr(card, number_field.name, '')))}")
        if balance_field is not None:
            lines.append(f"{balance_field.verbose_name}: {getattr(card, balance_field.name, '')}")
        if expire_field is not None:
            lines.append(f"{expire_field.verbose_name}: {self._format_scalar(getattr(card, expire_field.name, ''))}")
        if phone_field is not None:
            lines.append(f"{phone_field.verbose_name}: {self._format_scalar(getattr(card, phone_field.name, ''))}")

        return " | ".join(part for part in lines if part and not part.endswith(": "))

    def _list_user_card_choices(self, chat_id: int) -> str:
        cards = list(self._get_user_cards(chat_id)[:10])
        if not cards:
            return self._text(chat_id, "Ваших карт нет.", "You have no cards.")
        return "\n".join(f"- {self._card_label(card)}" for card in cards)

    def _card_label(self, card: Model) -> str:
        number_field = self.card_field_map.get("number")
        if number_field is not None:
            value = getattr(card, number_field.name, None)
            if value:
                return f"#{card.pk} {card_mask(str(value))}"
        return f"#{card.pk}"

    def _allowed_card_prefixes(self) -> tuple[str, ...]:
        raw_prefixes = str(_get_setting("TELEGRAM_CARD_PREFIXES", default="8600, 9860"))
        prefixes = tuple(
            item.strip() for item in raw_prefixes.split(",") if item.strip()
        )
        return prefixes or ("8600", "9860")

    def _normalize_card_number(self, chat_id: int, label: str, raw_value: str) -> str:
        digits = re.sub(r"\D", "", raw_value)
        if len(digits) != 16:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    "номер карты должен содержать 16 цифр.",
                    "card number must contain 16 digits.",
                    "kartaraqami 16 ta raqamdan iborat bo'lishi kerak.",
                )
            )

        allowed_prefixes = self._allowed_card_prefixes()
        if not any(digits.startswith(prefix) for prefix in allowed_prefixes):
            allowed_text = ", ".join(allowed_prefixes)
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    f"номер карты должен начинаться с одного из префиксов: {allowed_text}.",
                    f"card number must start with one of these prefixes: {allowed_text}.",
                    f"karta raqami quyidagi prefikslardan biri bilan boshlanishi kerak: {allowed_text}.",
                )
            )

        if not validate_card(digits):
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    "номер карты не прошел проверку алгоритмом Луна.",
                    "card number did not pass the Luhn checksum validation.",
                    "karta raqami Luhn tekshiruvidan o'tmadi.",
                )
            )

        return digits

    def _normalize_uz_phone(self, chat_id: int, label: str, raw_value: str) -> str:
        digits = re.sub(r"\D", "", raw_value)
        if digits.startswith("998") and len(digits) == 12:
            digits = digits[3:]
        elif len(digits) == 9:
            digits = digits
        else:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    "номер телефона должен быть в формате +998901234567 или 901234567.",
                    "phone must look like +998901234567 or 901234567.",
                    "telefon +998901234567 yoki 901234567 formatida bo'lishi kerak.",
                )
            )
        if not UZ_PHONE_RE.match(digits):
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                        label,
                        "введите корректный номер телефона Узбекистана.",
                        "enter a valid Uzbekistan phone number.",
                    "O'zbekiston telefon raqamini to'g'ri kiriting.",
                )
            )

        return f"+998{digits}"

    def _parse_filter_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter | None:
        raw_value = raw_value.strip()
        if raw_value.lower() in SKIP_TOKENS:
            return None

        if getattr(field, "choices", None):
            return self._parse_choice_value(chat_id, field, raw_value)
        if isinstance(field, BooleanField):
            return self._parse_boolean_value(chat_id, field, raw_value)
        if isinstance(
            field,
            (
                IntegerField,
                PositiveIntegerField,
                PositiveSmallIntegerField,
                PositiveBigIntegerField,
                SmallIntegerField,
                BigIntegerField,
                BigAutoField,
                SmallAutoField,
            ),
        ):
            return self._parse_integer_value(chat_id, field, raw_value)
        if isinstance(field, (FloatField, DecimalField)):
            return self._parse_decimal_value(chat_id, field, raw_value)
        if isinstance(field, DateTimeField):
            return self._parse_datetime_value(chat_id, field, raw_value)
        if isinstance(field, DateField):
            return self._parse_date_value(chat_id, field, raw_value)
        if isinstance(field, UUIDField):
            return self._parse_uuid_value(chat_id, field, raw_value)
        return self._parse_text_value(chat_id, field, raw_value)

    def _parse_card_field_value(self, chat_id: int, field_spec: FieldSpec, raw_value: str) -> tuple[Any, str]:
        raw_value = raw_value.strip()
        if raw_value.lower() in SKIP_TOKENS:
            if field_spec.required:
                raise ValidationProblem(
                    self._text(
                        chat_id,
                        "Это обязательное поле.",
                        "This field is required.",
                    )
                )
            return SKIPPED_VALUE, ""

        field = field_spec.field
        if field_spec.semantic == "card_number":
            digits = self._normalize_card_number(chat_id, field_spec.label, raw_value)
            cleaned = self._clean_field_value(chat_id, field, digits)
            return cleaned, self._mask_card_number(str(cleaned))

        if field_spec.semantic == "phone":
            normalized = self._normalize_uz_phone(chat_id, field_spec.label, raw_value)
            cleaned = self._clean_field_value(chat_id, field, normalized)
            return cleaned, str(cleaned)

        if field_spec.semantic == "expire":
            cleaned = self._parse_expire_field(chat_id, field_spec, raw_value)
            return cleaned, self._format_scalar(cleaned)

        if field_spec.semantic == "balance":
            amount = self._parse_positive_decimal(chat_id, field_spec.label, raw_value, zero_allowed=True)
            cleaned = self._clean_field_value(chat_id, field, amount)
            return cleaned, str(cleaned)

        if isinstance(field, BooleanField):
            parsed = self._parse_boolean_value(chat_id, field, raw_value)
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        if isinstance(
            field,
            (
                IntegerField,
                PositiveIntegerField,
                PositiveSmallIntegerField,
                PositiveBigIntegerField,
                SmallIntegerField,
                BigIntegerField,
                BigAutoField,
                SmallAutoField,
            ),
        ):
            parsed = self._parse_integer_value(chat_id, field, raw_value)
            if len(parsed.lookups) != 1:
                raise ValidationProblem(
                    self._text(
                        chat_id,
                        "Для добавления карты нужен один числовой параметр, а не диапазон.",
                        "Card creation requires a single number, not a range.",
                    )
                )
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        if isinstance(field, (FloatField, DecimalField)):
            parsed = self._parse_decimal_value(chat_id, field, raw_value)
            if len(parsed.lookups) != 1:
                raise ValidationProblem(
                    self._text(
                        chat_id,
                        "Для добавления карты нужно одно число, а не диапазон.",
                        "Card creation requires a single number, not a range.",
                    )
                )
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        if isinstance(field, DateTimeField):
            parsed = self._parse_datetime_value(chat_id, field, raw_value)
            if len(parsed.lookups) != 1:
                raise ValidationProblem(
                    self._text(
                        chat_id,
                        "Для добавления карты требуется одна дата/время, а не диапазон.",
                        "Card creation requires a single datetime, not a range.",
                    )
                )
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        if isinstance(field, DateField):
            parsed = self._parse_date_value(chat_id, field, raw_value)
            if len(parsed.lookups) != 1:
                raise ValidationProblem(
                    self._text(
                        chat_id,
                        "Для добавления карты требуется одна дата, а не диапазон.",
                        "Card creation requires a single date, not a range.",
                    )
                )
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        if isinstance(field, UUIDField):
            parsed = self._parse_uuid_value(chat_id, field, raw_value)
            value = next(iter(parsed.lookups.values()))
            return value, parsed.display_value

        parsed = self._parse_text_value(chat_id, field, raw_value)
        value = next(iter(parsed.lookups.values()))
        return value, parsed.display_value

    def _parse_choice_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        choices = dict(field.choices)
        normalized = raw_value.casefold()
        for key, label in choices.items():
            if normalized in {str(key).casefold(), str(label).casefold()}:
                cleaned = self._clean_field_value(chat_id, field, key)
                return ParsedFilter({field.name: cleaned}, str(label))

        available = ", ".join(str(label) for label in list(choices.values())[:8])
        raise ValidationProblem(
            self._validation_message(
                chat_id,
                str(field.verbose_name),
                f"недопустимое значение. Доступно: {available}.",
                f"invalid value. Available: {available}.",
            )
        )

    def _parse_boolean_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        lowered = raw_value.casefold()
        truthy = {"true", "1", "yes", "y", "on", "да", "д"}
        falsy = {"false", "0", "no", "n", "off", "нет", "н"}
        if lowered in truthy:
            cleaned = self._clean_field_value(chat_id, field, True)
            return ParsedFilter({field.name: cleaned}, self._text(chat_id, "Да", "Yes"))
        if lowered in falsy:
            cleaned = self._clean_field_value(chat_id, field, False)
            return ParsedFilter({field.name: cleaned}, self._text(chat_id, "Нет", "No"))

        raise ValidationProblem(
            self._validation_message(
                chat_id,
                str(field.verbose_name),
                "введите да или нет.",
                "enter yes or no.",
            )
        )

    def _parse_integer_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        range_match = INTEGER_RANGE_RE.match(raw_value)
        positive_field = isinstance(
            field,
            (PositiveIntegerField, PositiveSmallIntegerField, PositiveBigIntegerField),
        )

        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start > end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "начало диапазона должно быть меньше или равно концу.",
                        "range start must be less than or equal to range end.",
                    )
                )
            if positive_field and (start < 0 or end < 0):
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "поле принимает только положительные числа.",
                        "the field accepts only positive numbers.",
                    )
                )
            return ParsedFilter(
                {f"{field.name}__gte": start, f"{field.name}__lte": end},
                f"{start}..{end}",
            )

        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "некорректное целое число.",
                    "invalid whole number.",
                )
            ) from exc

        if positive_field and value < 0:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "поле принимает только положительные числа.",
                    "the field accepts only positive numbers.",
                )
            )

        cleaned = self._clean_field_value(chat_id, field, value)
        return ParsedFilter({field.name: cleaned}, str(cleaned))

    def _parse_decimal_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        range_match = NUMBER_RANGE_RE.match(raw_value)
        if range_match:
            try:
                start = Decimal(range_match.group(1))
                end = Decimal(range_match.group(2))
            except InvalidOperation as exc:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "некорректное число.",
                        "invalid number.",
                    )
                ) from exc
            if start > end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "начало диапазона должно быть меньше или равно концу.",
                        "range start must be less than or equal to range end.",
                    )
                )
            return ParsedFilter(
                {f"{field.name}__gte": start, f"{field.name}__lte": end},
                f"{start}..{end}",
            )

        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "некорректное число.",
                    "invalid number.",
                )
            ) from exc

        cleaned = self._clean_field_value(chat_id, field, value)
        return ParsedFilter({field.name: cleaned}, str(cleaned))

    def _parse_date_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        range_match = DATE_RANGE_RE.match(raw_value)
        if range_match:
            start = parse_date(range_match.group(1))
            end = parse_date(range_match.group(2))
            if not start or not end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "некорректный диапазон дат. Используйте YYYY-MM-DD..YYYY-MM-DD.",
                        "invalid date range. Use YYYY-MM-DD..YYYY-MM-DD.",
                    )
                )
            if start > end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "начальная дата должна быть раньше конечной.",
                        "date range start must be before the end.",
                    )
                )
            return ParsedFilter(
                {f"{field.name}__gte": start, f"{field.name}__lte": end},
                f"{start.isoformat()}..{end.isoformat()}",
            )

        parsed = parse_date(raw_value)
        if not parsed:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "некорректная дата. Используйте YYYY-MM-DD.",
                    "invalid date. Use YYYY-MM-DD.",
                )
            )

        cleaned = self._clean_field_value(chat_id, field, parsed)
        return ParsedFilter({field.name: cleaned}, cleaned.isoformat())

    def _parse_datetime_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        range_match = DATETIME_RANGE_RE.match(raw_value)
        if range_match:
            start = parse_datetime(range_match.group(1).replace(" ", "T"))
            end = parse_datetime(range_match.group(2).replace(" ", "T"))
            if not start or not end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "некорректный диапазон даты и времени.",
                        "invalid datetime range.",
                    )
                )
            if start > end:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        str(field.verbose_name),
                        "начало диапазона должно быть раньше конца.",
                        "datetime range start must be before the end.",
                    )
                )
            return ParsedFilter(
                {f"{field.name}__gte": start, f"{field.name}__lte": end},
                f"{start.isoformat(sep=' ')}..{end.isoformat(sep=' ')}",
            )

        parsed_datetime = parse_datetime(raw_value.replace(" ", "T"))
        if parsed_datetime:
            cleaned = self._clean_field_value(chat_id, field, parsed_datetime)
            return ParsedFilter({field.name: cleaned}, cleaned.isoformat(sep=" "))

        parsed_date = parse_date(raw_value)
        if parsed_date:
            return ParsedFilter({f"{field.name}__date": parsed_date}, parsed_date.isoformat())

        raise ValidationProblem(
            self._validation_message(
                chat_id,
                str(field.verbose_name),
                "некорректная дата и время.",
                "invalid datetime.",
            )
        )

    def _parse_uuid_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        try:
            value = UUID(raw_value)
        except ValueError as exc:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "некорректный UUID.",
                    "invalid UUID.",
                )
            ) from exc

        cleaned = self._clean_field_value(chat_id, field, value)
        return ParsedFilter({field.name: cleaned}, str(cleaned))

    def _parse_text_value(self, chat_id: int, field: Any, raw_value: str) -> ParsedFilter:
        max_length = getattr(field, "max_length", None)
        if not raw_value:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    "значение не может быть пустым.",
                    "value cannot be empty.",
                )
            )
        if max_length and len(raw_value) > max_length:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    str(field.verbose_name),
                    f"значение слишком длинное. Максимум {max_length} символов.",
                    f"value is too long. Maximum is {max_length} characters.",
                )
            )

        cleaned = self._clean_field_value(chat_id, field, raw_value)
        return ParsedFilter({f"{field.name}__icontains": cleaned}, str(cleaned))

    def _parse_expire_field(self, chat_id: int, field_spec: FieldSpec, raw_value: str) -> Any:
        field = field_spec.field
        expiry_match = EXPIRY_RE.match(raw_value)
        if expiry_match:
            month = int(expiry_match.group(1))
            year = int(expiry_match.group(2))
            if year < 100:
                year += 2000
            last_day = calendar.monthrange(year, month)[1]
            expiry_date = date(year, month, last_day)
            if isinstance(field, DateField) and not isinstance(field, DateTimeField):
                return self._clean_field_value(chat_id, field, expiry_date)
            normalized = f"{month:02d}/{str(year)[-2:]}"
            return self._clean_field_value(chat_id, field, normalized)

        if isinstance(field, DateField):
            parsed_date = parse_date(raw_value)
            if not parsed_date:
                raise ValidationProblem(
                    self._validation_message(
                        chat_id,
                        field_spec.label,
                        "некорректная дата срока действия.",
                        "invalid expiry date.",
                    )
                )
            return self._clean_field_value(chat_id, field, parsed_date)

        max_length = getattr(field, "max_length", None)
        if max_length and len(raw_value) > max_length:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    field_spec.label,
                    f"значение слишком длинное. Максимум {max_length} символов.",
                    f"value is too long. Maximum is {max_length} characters.",
                )
            )
        return self._clean_field_value(chat_id, field, raw_value)

    def _parse_positive_decimal(
        self,
        chat_id: int,
        label: str,
        raw_value: str,
        zero_allowed: bool = False,
    ) -> Decimal:
        try:
            amount = Decimal(raw_value)
        except InvalidOperation as exc:
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    "некорректное число.",
                    "invalid number.",
                )
            ) from exc

        if amount < 0 or (amount == 0 and not zero_allowed):
            raise ValidationProblem(
                self._validation_message(
                    chat_id,
                    label,
                    "значение должно быть положительным.",
                    "value must be positive.",
                )
            )
        return amount

    def _parse_transfer_amount(self, chat_id: int, raw_value: str) -> Decimal:
        return self._parse_positive_decimal(
            chat_id,
            self._text(chat_id, "Сумма", "Amount"),
            raw_value,
            zero_allowed=False,
        )

    def _clean_field_value(self, chat_id: int, field: Any, value: Any) -> Any:
        try:
            return field.clean(value, None)
        except ValidationError as exc:
            raise ValidationProblem(
                self._text(
                    chat_id,
                    f'Ошибка валидации поля "{field.verbose_name}": {"; ".join(exc.messages)}',
                    f'Validation error for field "{field.verbose_name}": {"; ".join(exc.messages)}',
                )
            ) from exc

    def _coerce_chat_id(self, field: Any, chat_id: int) -> Any:
        if isinstance(
            field,
            (
                IntegerField,
                PositiveIntegerField,
                PositiveSmallIntegerField,
                PositiveBigIntegerField,
                SmallIntegerField,
                BigIntegerField,
            ),
        ):
            return int(chat_id)
        return str(chat_id)

    def _resolve_card_reference(self, chat_id: int, raw_value: str, only_user_cards: bool) -> Model:
        value = raw_value.strip()
        queryset = self.card_model.objects.all()
        if only_user_cards:
            queryset = self._get_user_cards(chat_id)

        if value.isdigit():
            by_id = queryset.filter(pk=int(value)).first()
            if by_id is not None:
                return by_id

        number_field = self.card_field_map.get("number")
        if number_field is not None:
            normalized = re.sub(r"\D", "", value)
            if normalized:
                if len(normalized) == 16 and not validate_card(normalized):
                    raise ValidationProblem(
                        self._text(
                            chat_id,
                            "Номер карты не прошел проверку алгоритмом Луна.",
                            "Card number failed Luhn checksum validation.",
                        )
                    )

                exact = queryset.filter(**{number_field.name: normalized}).first()
                if exact is not None:
                    return exact
                for card in queryset[:100]:
                    current = re.sub(r"\D", "", str(getattr(card, number_field.name, "")))
                    if current == normalized:
                        return card
                    if len(normalized) == 4 and current.endswith(normalized):
                        return card

        raise ValidationProblem(
            self._text(
                chat_id,
                "Карта не найдена. Укажите корректный ID, номер карты или последние 4 цифры.",
                "Card not found. Enter a valid ID, card number, or last 4 digits.",
            )
        )


    def _get_user_cards(self, chat_id: int) -> Any:
        owner_field = self.card_field_map.get("owner")
        if owner_field is None:
            return self.card_model.objects.none()
        return self.card_model.objects.filter(
            **{owner_field.name: self._coerce_chat_id(owner_field, chat_id)}
            **{owner_field.name: self._coerce_chat_id(owner_field, chat_id)}
        )

    def _resolve_filter_model(self) -> type[Model]:
        configured = _get_setting("TELEGRAM_FILTER_MODEL")
        if configured:
            return self._get_model_from_string(configured)
        return self._pick_best_model(prefer_card=False)
        

    

    def _resolve_card_model(self) -> type[Model] | None:
        configured = _get_setting("TELEGRAM_CARD_MODEL")
        if configured:
            return self._get_model_from_string(configured)

        if "card" in self.filter_model.__name__.lower():
            return self.filter_model

        candidates = self._candidate_models()
        card_candidates = [model for model in candidates if "card" in model.__name__.lower()]
        if card_candidates:
            card_candidates.sort(key=lambda model: model.__name__.lower())
            return card_candidates[0]
        return None

    def _get_model_from_string(self, configured: str) -> type[Model]:
        try:
            app_label, model_name = configured.split(".", 1)
        except ValueError as exc:
            raise RuntimeError(
                "Model setting must look like 'app_label.ModelName'."
            ) from exc

        model = apps.get_model(app_label, model_name)
        if model is None:
            raise RuntimeError(f"Could not resolve model {configured!r}.")
        return model

    def _candidate_models(self) -> list[type[Model]]:
        preferred_app = "app"
        candidates: list[type[Model]] = []
        try:
            candidates.extend(list(apps.get_app_config(preferred_app).get_models()))
        except LookupError:
            pass
        if not candidates:
            candidates.extend(list(apps.get_models()))

        return [
            model
            for model in candidates
            if not model._meta.abstract
            and not model._meta.proxy
            and model._meta.managed
            and model._meta.app_label not in {"auth", "admin", "contenttypes", "sessions"}
        ]

    def _pick_best_model(self, prefer_card: bool) -> type[Model]:
        models = self._candidate_models()
        if not models:
            raise RuntimeError("No usable Django models were found.")

        def score(model: type[Model]) -> tuple[int, int, str]:
            scalar_fields = [
                field
                for field in model._meta.concrete_fields
                if not field.primary_key and not field.is_relation
            ]
            card_bonus = -10 if prefer_card and "card" in model.__name__.lower() else 0
            return (card_bonus - len(scalar_fields), len(model._meta.fields), model.__name__.lower())

        models.sort(key=score)
        return models[0]

    def _resolve_filter_fields(self, model: type[Model]) -> list[FieldSpec]:
        configured_fields = _get_setting("TELEGRAM_FILTER_FIELDS")
        if configured_fields:
            if isinstance(configured_fields, str):
                field_names = [item.strip() for item in configured_fields.split(",") if item.strip()]
            else:
                field_names = list(configured_fields)
        else:
            max_fields = int(_get_setting("TELEGRAM_MAX_FILTER_FIELDS", default=8))
            field_names = [
                field.name
                for field in model._meta.concrete_fields
                if self._is_filterable_field(field)
            ][:max_fields]

        field_specs: list[FieldSpec] = []
        for field_name in field_names:
            field = model._meta.get_field(field_name)
            if not self._is_filterable_field(field):
                continue
            field_specs.append(
                FieldSpec(
                    field_name=field.name,
                    label=str(field.verbose_name).replace("_", " ").title(),
                    field=field,
                )
            )
        if not field_specs:
            raise RuntimeError(f"No filterable fields found on model {model.__name__!r}.")
        return field_specs

    def _resolve_result_fields(self, model: type[Model]) -> list[Any]:
        configured_fields = _get_setting("TELEGRAM_RESULT_FIELDS")
        if configured_fields:
            if isinstance(configured_fields, str):
                field_names = [item.strip() for item in configured_fields.split(",") if item.strip()]
            else:
                field_names = list(configured_fields)
        else:
            priority = ["title", "name", "card_number", "balance", "phone", "expire", "created_at"]
            available_fields = {
                field.name: field
                for field in model._meta.concrete_fields
                if not field.is_relation
            }
            field_names = [field_name for field_name in priority if field_name in available_fields]
            if not field_names:
                field_names = list(available_fields)[:4]
        return [model._meta.get_field(field_name) for field_name in field_names]

    def _build_card_field_map(self) -> dict[str, Any]:
        if self.card_model is None:
            return {}

        fields = [
            field
            for field in self.card_model._meta.concrete_fields
            if not field.primary_key and not field.is_relation
        ]

        return {
            "owner": self._find_field(fields, ("telegram_chat_id", "chat_id", "telegram", "telegram_id")),
            "number": self._find_field(fields, ("card_number", "number", "pan", "card")),
            "balance": self._find_field(fields, ("balance", "amount", "money")),
            "expire": self._find_field(fields, ("expire", "expiry", "exp")),
            "phone": self._find_field(fields, ("phone", "mobile", "tel")),
            "holder": self._find_field(fields, ("holder", "owner_name", "fullname", "name")),
        }

    def _find_field(self, fields: list[Any], keywords: tuple[str, ...]) -> Any | None:
        best_field = None
        best_score = -1
        for field in fields:
            name = field.name.lower()
            score = 0
            for keyword in keywords:
                if name == keyword:
                    score += 10
                elif keyword in name:
                    score += 4
            if score > best_score:
                best_field = field
                best_score = score
        return best_field if best_score > 0 else None

    def _resolve_add_card_fields(self) -> list[FieldSpec]:
        if self.card_model is None:
            return []

        owner_field = self.card_field_map.get("owner")
        chosen: list[FieldSpec] = []
        seen: set[str] = set()

        ordered_semantics = [
            ("number", "card_number"),
            ("holder", "holder"),
            ("phone", "phone"),
            ("expire", "expire"),
            ("balance", "balance"),
        ]
        for map_key, semantic in ordered_semantics:
            field = self.card_field_map.get(map_key)
            if field is None or field.name in seen or field == owner_field:
                continue
            chosen.append(self._make_card_field_spec(field, semantic))
            seen.add(field.name)

        for field in self.card_model._meta.concrete_fields:
            if (
                field.primary_key
                or field.auto_created
                or field.is_relation
                or not getattr(field, "editable", True)
                or field.name in seen
                or field == owner_field
            ):
                continue
            if not self._is_required_for_creation(field):
                continue
            chosen.append(self._make_card_field_spec(field, "generic"))
            seen.add(field.name)

        return chosen

    def _make_card_field_spec(self, field: Any, semantic: str) -> FieldSpec:
        return FieldSpec(
            field_name=field.name,
            label=str(field.verbose_name).replace("_", " ").title(),
            field=field,
            required=self._is_required_for_creation(field),
            semantic=semantic,
        )

    def _is_required_for_creation(self, field: Any) -> bool:
        has_default = field.has_default() or getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
        return not field.null and not getattr(field, "blank", False) and not has_default

    def _is_filterable_field(self, field: Any) -> bool:
        if field.primary_key or field.auto_created or field.is_relation:
            return False

        allowed_types = (
            BooleanField,
            CharField,
            DateField,
            DateTimeField,
            DecimalField,
            FloatField,
            IntegerField,
            PositiveBigIntegerField,
            PositiveIntegerField,
            PositiveSmallIntegerField,
            SmallIntegerField,
            BigIntegerField,
            TextField,
            UUIDField,
            BigAutoField,
            SmallAutoField,
        )
        return isinstance(field, allowed_types) or bool(getattr(field, "choices", None))

    def _apply_default_ordering(self, queryset: Any) -> Any:
        configured = _get_setting("TELEGRAM_RESULT_ORDERING")
        if configured:
            if isinstance(configured, str):
                ordering = [item.strip() for item in configured.split(",") if item.strip()]
            else:
                ordering = list(configured)
            if ordering:
                return queryset.order_by(*ordering)
        return queryset.order_by("-pk")

    def _format_scalar(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat(sep=" ", timespec="minutes")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value).strip()


def _chunk_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0

    for line in text.splitlines():
        if len(line) > limit:
            if current_lines:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []
                current_length = 0
            for index in range(0, len(line), limit):
                piece = line[index : index + limit].strip()
                if piece:
                    chunks.append(piece)
            continue

        line_length = len(line) + 1
        if current_lines and current_length + line_length > limit:
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
            current_length = line_length
            continue

        current_lines.append(line)
        current_length += line_length

    if current_lines:
        chunks.append("\n".join(current_lines).strip())

    return chunks


def main() -> None:
    bootstrap_django()

    logging.basicConfig(
        level=_get_setting("TELEGRAM_LOG_LEVEL", default="INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = DjangoTelegramBot(_build_config())
    bot.run()
