"""
Хранилище прогнозов и настроек через GitHub Gist.

ЛОГИКА:
- Прогнозы — ОБЩИЕ для всех авторизованных пользователей.
  Хранятся под ключом "shared" в predictions.json.
- Настройки — ИНДИВИДУАЛЬНЫЕ для каждого пользователя (часовой пояс).
"""
import threading
import logging
import hashlib
import copy
from datetime import datetime, timedelta, date, time as dtime
from typing import Optional
from models import Prediction, UserSettings
import config
import gist_storage
import match_dates
from match_dates import resolve_match_datetime, to_local, local_to_utc, retention_minutes
from parser import extract_raw_matches

logger = logging.getLogger(__name__)

FILE_PREDS    = gist_storage.FILE_PREDICTIONS
FILE_SETTINGS = gist_storage.FILE_SETTINGS

# Единый ключ для общего списка прогнозов
SHARED_KEY = "shared"

_lock = threading.RLock()


# ── Predictions (общие для всех) ─────────────────────────────────────────────

def load_predictions(chat_id: int = None) -> list[Prediction]:
    """chat_id игнорируется — список общий."""
    data = gist_storage.read(FILE_PREDS)

    # Авто-миграция: если есть данные под старыми ключами (chat_id), но нет под "shared" —
    # объединяем всё в один общий список
    if SHARED_KEY not in data and any(k.lstrip("-").isdigit() for k in data.keys()):
        with _lock:
            data = gist_storage.read(FILE_PREDS)
            merged = []
            seen_texts = set()
            for key, items in list(data.items()):
                if key == SHARED_KEY:
                    continue
                if not key.lstrip("-").isdigit():
                    continue
                for item in items:
                    text_key = item.get("text", "").strip().lower()
                    if text_key and text_key not in seen_texts:
                        seen_texts.add(text_key)
                        merged.append(item)
                del data[key]
            merged.sort(key=lambda p: p.get("match_time", ""))
            data[SHARED_KEY] = merged
            gist_storage.write(FILE_PREDS, data)
            logger.info(f"Миграция: объединено {len(merged)} прогнозов в общий список")

    return [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]


def save_predictions(chat_id: int = None, predictions: list[Prediction] = None):
    """chat_id игнорируется — список общий."""
    if predictions is None:
        predictions = []
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        predictions = sorted(predictions, key=lambda p: p.match_time)
        data[SHARED_KEY] = [p.to_dict() for p in predictions]
        gist_storage.write(FILE_PREDS, data)


def add_predictions(chat_id: int = None, new_preds: list[Prediction] = None) -> int:
    """Добавляет в общий список. Пропускает дубли по тексту."""
    if not new_preds:
        return 0
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        existing = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        existing_texts = {p.text.strip().lower() for p in existing}

        added = []
        for p in new_preds:
            if p.text.strip().lower() not in existing_texts:
                added.append(p)
                existing_texts.add(p.text.strip().lower())

        if added:
            all_preds = sorted(existing + added, key=lambda p: p.match_time)
            data[SHARED_KEY] = [p.to_dict() for p in all_preds]
            gist_storage.write(FILE_PREDS, data)
        return len(added)


def _match_key(pred: Prediction) -> str:
    """
    Ключ матча для сравнения «тот же матч»: команды + время.
    Команды берём из текста, нормализуем (убираем регистр, лишние пробелы).
    """
    import re
    from fonbet import extract_teams_from_prediction
    t1, t2 = extract_teams_from_prediction(pred.text)
    # Нормализация: нижний регистр, только буквы/цифры
    def norm(s):
        return re.sub(r'[^a-zа-я0-9]', '', (s or "").lower())
    teams = "|".join(sorted([norm(t1), norm(t2)]))  # сорт чтобы порядок команд не влиял
    # Время с точностью до минуты
    tm = pred.match_time.strftime("%Y%m%d%H%M")
    return f"{teams}@{tm}"


def sync_from_discord(discord_preds: list[Prediction]) -> dict:
    """
    Полная синхронизация общего листа с актуальными прогнозами Discord.
    - новый матч (нет в листе) → добавить
    - матч есть, текст изменился → обновить текст, сбросить crooked_notified
    - discord-матч пропал из Discord и ещё НЕ начался → удалить

    Ручные прогнозы (source != 'discord') не трогаем при удалении.
    Возвращает статистику: {added, updated, removed}.

    LEGACY: Discord-слушатель больше её не вызывает — ключ матча здесь включает
    дату, посчитанную как «сегодня на момент парсинга», из-за чего вчерашние
    сообщения попадали в сегодняшнее расписание. Актуальная логика —
    apply_discord_messages() ниже. Функция оставлена для совместимости.
    """
    from datetime import datetime

    with _lock:
        data = gist_storage.read(FILE_PREDS)
        existing = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]

        # Индексируем существующие по ключу матча
        existing_by_key = {}
        for p in existing:
            existing_by_key[_match_key(p)] = p

        # Индексируем свежие из Discord
        discord_by_key = {}
        for p in discord_preds:
            discord_by_key[_match_key(p)] = p

        added = updated = removed = 0
        result = []
        now = datetime.utcnow()

        # 1. Проходим существующие: оставляем/обновляем/удаляем
        for key, old in existing_by_key.items():
            if key in discord_by_key:
                # Матч всё ещё в Discord — проверяем не изменился ли текст
                new = discord_by_key[key]
                if old.text.strip() != new.text.strip():
                    # Текст обновился (например дописали ставку) — обновляем
                    old.text = new.text
                    old.crooked_notified = False  # пересчитать кривизну
                    updated += 1
                result.append(old)
            else:
                # Матча нет в Discord
                is_discord = (old.source == "discord")
                not_started = old.match_time > now
                if is_discord and not_started:
                    # Удаляем (пропал из Discord, ещё не начался)
                    removed += 1
                    continue
                else:
                    # Ручной прогноз или уже начавшийся — оставляем
                    result.append(old)

        # 2. Добавляем новые матчи из Discord которых не было
        added_preds = []
        for key, new in discord_by_key.items():
            if key not in existing_by_key:
                result.append(new)
                added_preds.append(new)
                added += 1

        result.sort(key=lambda p: p.match_time)
        data[SHARED_KEY] = [p.to_dict() for p in result]
        gist_storage.write(FILE_PREDS, data)

        logger.info(f"sync_from_discord: +{added} ~{updated} -{removed}")
        return {
            "added": added, "updated": updated, "removed": removed,
            "added_preds": added_preds,   # сами добавленные прогнозы для уведомления
        }



# ── Discord: сообщения → прогнозы (дата матча, а не возраст сообщения) ───────
#
# Реестр сообщений («ledger») хранится в том же predictions.json под ключом
# LEDGER_KEY (load_predictions его не читает и не трогает, а save_predictions
# сохраняет — он перезаписывает только ключ "shared"). Для каждого сообщения
# Discord (по постоянному message_id) там записано:
#   created_at / edited_at / hash          — версия, которую мы обработали;
#   matches[teams_key] = {status, time, anchor, pred_id, text, first_seen_at, ...}
# где anchor — момент, когда матч ПОЯВИЛСЯ в сообщении (публикация или правка);
# именно от него считается дата матча (см. match_dates). Дата определяется один
# раз и дальше не «плывёт» с течением времени. Реестр переживает чистку
# расписания планировщиком, поэтому сыгранные матчи не возвращаются, а матчи,
# дописанные правкой, получают правильную дату (по времени правки).
#
# Статусы матча: active (есть/был в расписании), stale (на момент появления уже
# прошёл), undetermined (дату достоверно определить нельзя), removed (автор
# убрал матч из сообщения).

LEDGER_KEY = "discord_ledger"
LEDGER_KEEP_HOURS = 96


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _ident(text: str) -> tuple[str, str]:
    """
    (ключ матча, нормализованная первая команда). Ключ — нормализованные команды без
    времени и даты; значки/эмодзи, дописанные автором в конец строки («✅», «🔥»),
    на ключ не влияют — иначе правка текста выглядела бы как «новый матч».
    """
    import re
    from fonbet import extract_teams_from_prediction
    # выбрасываем токены-«значки» (без букв/цифр), но тире-разделитель команд оставляем
    cleaned = " ".join(tok for tok in text.split() if re.search(r"\w", tok) or tok in ("—", "–", "-"))
    t1, t2 = extract_teams_from_prediction(cleaned)

    def norm(x):
        return re.sub(r'[^a-zа-я0-9]', '', (x or "").lower())

    if not norm(t1) and not norm(t2):
        # команды не выделились — идентифицируем по тексту целиком
        return "text:" + norm(text), ""
    return "|".join(sorted([norm(t1), norm(t2)])), norm(t1)


def teams_key(text: str) -> str:
    """Идентификатор матча внутри сообщения (см. _ident)."""
    return _ident(text)[0]


def _h8(key: str) -> str:
    """Короткий отпечаток ключа матча — для callback_data кнопок (лимит Telegram 64 байта)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


class _DiscordPass:
    """Один проход синхронизации. Всегда выполняется под _lock, читает свежие данные."""

    def __init__(self, preds, ledger, now, tz):
        self.preds: list[Prediction] = preds
        self.by_id = {p.id: p for p in preds}
        self.ledger: dict = ledger
        self.now: datetime = now
        self.tz: int = tz
        self.cutoff = now - timedelta(minutes=retention_minutes(getattr(config, "DELETE_AFTER_MINUTES", 5)))
        self.added_preds: list[Prediction] = []
        self.undetermined_new: list[dict] = []   # матчи без достоверной даты — спросить админа
        self._tk_cache: dict[str, str] = {}
        self.stats = {
            "processed_messages": 0, "unchanged": 0, "outdated_event": 0,
            "found": 0, "added": 0, "updated": 0, "removed": 0, "restored": 0,
            "skipped_stale": 0, "skipped_undetermined": 0, "skipped_duplicate": 0,
            "legacy_removed": 0,
        }

    # ── утилиты ──
    def _fmt(self, dt: Optional[datetime]) -> str:
        if not dt:
            return "—"
        return f"{to_local(dt, self.tz):%d.%m %H:%M}"

    def _pred_tk(self, p: Prediction) -> str:
        if p.id not in self._tk_cache:
            self._tk_cache[p.id] = teams_key(p.text)
        return self._tk_cache[p.id]

    def _owned(self, p: Optional[Prediction], mid: str) -> bool:
        return bool(p) and p.source == "discord" and p.msg_id == mid

    def _remove_pred(self, p: Prediction):
        self.preds[:] = [x for x in self.preds if x.id != p.id]
        self.by_id.pop(p.id, None)

    def _add_pred(self, p: Prediction):
        self.preds.append(p)
        self.by_id[p.id] = p

    def _find_existing(self, key: str, dt: datetime) -> Optional[Prediction]:
        """Уже есть тот же матч (те же команды, то же время с точностью до минуты)?"""
        for p in self.preds:
            if _minute(p.match_time) == _minute(dt) and self._pred_tk(p) == key:
                return p
        return None

    # ── сообщение целиком ──
    def process(self, snap: dict):
        mid = str(snap["id"])
        created: datetime = snap["created_at"]
        edited: Optional[datetime] = snap.get("edited_at")
        content: str = snap.get("content") or ""
        version = edited or created
        chash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        head = f"Discord msg {mid} [создано {self._fmt(created)}, правка {self._fmt(edited)}]"

        E = self.ledger.get(mid)
        if E is not None:
            e_version = _from_iso(E.get("edited_at")) or _from_iso(E["created_at"])
            if version < e_version:
                # устаревший снимок (например перечит начался до правки, а обработан после)
                self.stats["outdated_event"] += 1
                logger.info(f"{head}: пропуск — снимок старее уже обработанной версии (гонка событий)")
                return
            if version == e_version and E.get("hash") == chash:
                self.stats["unchanged"] += 1
                self._ensure_present(mid, E)
                return

        self.stats["processed_messages"] += 1
        first_time = E is None
        if first_time:
            E = {"created_at": _iso(created), "first_seen_at": _iso(self.now), "matches": {}}
        local = {k: 0 for k in ("found", "added", "updated", "removed", "restored",
                                "skipped_stale", "skipped_undetermined", "skipped_duplicate")}
        before = dict(self.stats)

        raw = extract_raw_matches(content)
        self._rekey_renamed(E, raw)
        seen: set[str] = set()
        for r in raw:
            key = teams_key(r.text)
            if key in seen:
                self.stats["skipped_duplicate"] += 1
                logger.info(f"{head}: пропуск «{r.text[:60]}» — повтор матча внутри этого же сообщения")
                continue
            seen.add(key)
            self.stats["found"] += 1
            self._handle_match(mid, E, first_time, created, edited, r, key, head)

        # матчи, пропавшие из сообщения
        for key, rec in E["matches"].items():
            if key not in seen and rec.get("status") != "removed":
                self._retract(mid, key, rec, head, "убран из сообщения")

        E["edited_at"] = _iso(edited)
        E["hash"] = chash
        E["last_processed_at"] = _iso(self.now)
        self.ledger[mid] = E

        local = {k: self.stats[k] - before[k] for k in local}
        logger.info(
            f"{head}: найдено {local['found']}, новых {local['added']}, обновлено {local['updated']}, "
            f"убрано {local['removed']}, восстановлено {local['restored']}; пропущено: "
            f"прошедших {local['skipped_stale']}, с неясной датой {local['skipped_undetermined']}, "
            f"дублей {local['skipped_duplicate']}"
        )

    def _rekey_renamed(self, E, raw):
        """
        Автор поправил название команды (опечатка, дописал/убрал слово), а время то же:
        ключ матча сменился, но это тот же матч. Переносим запись реестра на новый ключ —
        иначе правка считалась бы «удалением одного матча и добавлением другого».
        Условие осторожное: совпадают первая команда и время суток, а старый ключ из
        сообщения исчез.
        """
        new_keys = {teams_key(r.text) for r in raw}
        orphans = {k: rec for k, rec in E["matches"].items()
                   if k not in new_keys and rec.get("status") != "removed"}
        if not orphans:
            return
        for r in raw:
            key, t1 = _ident(r.text)
            if key in E["matches"] or not t1:
                continue
            hm = f"{r.hour:02d}:{r.minute:02d}"
            for ok, rec in list(orphans.items()):
                if rec.get("t1") == t1 and rec.get("hm") == hm:
                    E["matches"][key] = E["matches"].pop(ok)
                    orphans.pop(ok)
                    break

    # ── один матч ──
    def _resolve_new(self, r, first_logic, created, edited):
        """
        Дата матча, у которого нет надёжного anchor в реестре. → (Resolution, anchor|None, cold, note).
        first_logic=True — сообщение (или этот матч) бот видит впервые; False — матч дописан
        правкой уже известного сообщения.
        """
        if not first_logic:
            anchor = edited or created
            res = resolve_match_datetime(r.hour, r.minute, r.hint, anchor, self.tz)
            return res, anchor, False, f"появился правкой, anchor=правка {self._fmt(anchor)}"
        if edited is None:
            res = resolve_match_datetime(r.hour, r.minute, r.hint, created, self.tz)
            return res, created, False, f"первая версия, anchor=публикация {self._fmt(created)}"
        # Холодный старт на уже отредактированном сообщении: что было в оригинале,
        # а что дописано правкой — неизвестно. Принимаем дату только если обе
        # версии (по публикации и по правке) её не оспаривают.
        rc = resolve_match_datetime(r.hour, r.minute, r.hint, created, self.tz)
        re_ = resolve_match_datetime(r.hour, r.minute, r.hint, edited, self.tz)
        if rc.dt is not None and (re_.dt is None or re_.dt == rc.dt):
            return rc, created, False, "сообщение уже было отредактировано; даты по публикации и по правке не противоречат"
        if rc.dt is None and re_.dt is None:
            return rc, None, True, "сообщение уже было отредактировано"
        return (
            match_dates.Resolution(
                None, match_dates.BASIS_UNDETERMINED,
                f"сообщение отредактировано до того, как бот его увидел: по публикации {self._fmt(rc.dt)}, "
                f"по правке {self._fmt(re_.dt)} — неизвестно, что было в оригинале, а что дописано",
            ),
            None, True, "холодный старт на правленом сообщении",
        )

    def _handle_match(self, mid, E, first_time, created, edited, r, key, head):
        rec = E["matches"].get(key)
        cold_rec = bool(rec and rec.get("cold"))
        label = f"«{r.text[:60]}»"

        if rec is not None and rec.get("manual_date"):
            # дату выбрал админ кнопкой (/undetermined) — она главнее любых правил;
            # время берём из текущей версии сообщения (его могли поправить)
            md = date.fromisoformat(rec["manual_date"])
            res = match_dates.Resolution(
                local_to_utc(datetime.combine(md, dtime(r.hour, r.minute)), self.tz),
                "admin", f"дата выбрана админом: {md.isoformat()}",
            )
            anchor = _from_iso(rec.get("anchor"))
            cold = False
            note = "дата выбрана админом"
        elif rec is not None and rec.get("anchor") and not cold_rec:
            # матч уже знаем (в т.ч. убранный и возвращённый автором — это тот же матч,
            # а не новый) — дата считается от сохранённого момента появления
            anchor = _from_iso(rec["anchor"])
            res = resolve_match_datetime(r.hour, r.minute, r.hint, anchor, self.tz)
            note = f"anchor из реестра {self._fmt(anchor)}"
            cold = False
        else:
            # нового матча в уже известном сообщении (rec is None, не первая версия) — anchor = правка;
            # в первой версии сообщения и для «холодных» записей — логика первого просмотра
            first_logic = first_time or cold_rec
            res, anchor, cold, note = self._resolve_new(r, first_logic, created, edited)

        old_status = rec.get("status") if rec else None
        new = {
            "status": None, "time": None, "anchor": _iso(anchor), "cold": cold,
            "pred_id": rec.get("pred_id") if rec else None, "text": r.text,
            "basis": res.basis, "first_seen_at": (rec or {}).get("first_seen_at") or _iso(self.now),
            "t1": _ident(r.text)[1], "hm": f"{r.hour:02d}:{r.minute:02d}",
        }
        if rec:
            for k in ("asked", "dismissed", "manual_date"):   # решения админа переживают пересчёт
                if k in rec:
                    new[k] = rec[k]

        # дата не определена
        if res.dt is None:
            new["status"] = "undetermined"
            new["reason"] = res.reason
            self.stats["skipped_undetermined"] += 1
            if old_status != "undetermined":
                logger.warning(f"{head}: {label} — дата НЕ определена ({res.reason}); в расписание не добавлен, "
                               f"будет разобран заново при правке сообщения")
            if not new.get("asked") and not new.get("dismissed"):
                new["asked"] = True     # админу уходит вопрос с кнопками ровно один раз
                self.undetermined_new.append({
                    "mid": mid, "key": key, "h8": _h8(key), "text": r.text,
                    "reason": res.reason, "hm": new["hm"], "created": created,
                })
            if old_status == "active":
                self._drop_owned_future(mid, rec, head, label, "время изменено, новая дата неопределённа")
            E["matches"][key] = new
            return

        dt = res.dt
        new["time"] = _iso(dt)

        # активный матч, который мы уже знаем → обновление
        if old_status == "active":
            self._update_known(mid, E, key, rec, new, dt, res, r, head, label)
            return

        # stale с тем же временем → без изменений
        if old_status == "stale" and rec.get("time") == _iso(dt):
            new["status"] = "stale"
            self.stats["skipped_stale"] += 1
            E["matches"][key] = new
            return

        # новый матч (или вернувшийся / ставший определённым / перенесённый из прошлого)
        if dt < self.cutoff:
            new["status"] = "stale"
            self.stats["skipped_stale"] += 1
            if old_status != "stale":
                logger.info(f"{head}: {label} — пропуск: матч {self._fmt(dt)} уже прошёл "
                            f"(основание даты: {res.basis}; {res.reason}; {note})")
            E["matches"][key] = new
            return

        existing = self._find_existing(key, dt)
        if existing is not None:
            if existing.source == "discord" and existing.msg_id is None:
                existing.msg_id = mid
                existing.date_basis = existing.date_basis or res.basis
                existing.first_seen_at = existing.first_seen_at or _iso(self.now)
                logger.info(f"{head}: {label} — {self._fmt(dt)} уже в расписании (запись до обновления), привязана к сообщению")
            else:
                self.stats["skipped_duplicate"] += 1
                logger.info(f"{head}: {label} — пропуск: дубликат, матч {self._fmt(dt)} уже в расписании "
                            f"(id {existing.id}, источник {existing.source})")
            new["status"] = "active"
            new["pred_id"] = existing.id
            E["matches"][key] = new
            return

        p = Prediction(
            text=r.text, match_time=dt, source="discord",
            msg_id=mid, first_seen_at=_iso(self.now), date_basis=res.basis,
        )
        if new["pred_id"] and new["pred_id"] not in self.by_id:
            p.id = new["pred_id"]
        self._add_pred(p)
        self.added_preds.append(p)
        self.stats["added"] += 1
        new["status"] = "active"
        new["pred_id"] = p.id
        E["matches"][key] = new
        logger.info(f"{head}: {label} — ДОБАВЛЕН на {self._fmt(dt)} "
                    f"(основание даты: {res.basis}; {res.reason}; {note})")

    def _update_known(self, mid, E, key, rec, new, dt, res, r, head, label):
        old_dt = _from_iso(rec.get("time"))
        p = self.by_id.get(rec.get("pred_id"))
        new["status"] = "active"
        time_changed = old_dt is None or _minute(old_dt) != _minute(dt)
        text_changed = (p.text if p else rec.get("text")) != r.text

        if dt < self.cutoff and time_changed:
            # время изменено на уже прошедшее — в активное расписание не возвращаем
            if self._owned(p, mid):
                self._remove_pred(p)
            new["status"] = "stale"
            self.stats["skipped_stale"] += 1
            E["matches"][key] = new
            logger.info(f"{head}: {label} — время изменено на {self._fmt(dt)}, это уже прошло — из расписания убран")
            return

        if p is None:
            # записи нет в расписании (чистка после старта / потеря записи)
            if dt >= self.cutoff and dt > self.now and not self._find_existing(key, dt):
                np_ = Prediction(text=r.text, match_time=dt, source="discord", msg_id=mid,
                                 first_seen_at=rec.get("first_seen_at"), date_basis=res.basis)
                if rec.get("pred_id"):
                    np_.id = rec["pred_id"]
                self._add_pred(np_)
                new["pred_id"] = np_.id
                self.stats["restored" if not time_changed else "added"] += 1
                if time_changed:
                    self.added_preds.append(np_)
                logger.info(f"{head}: {label} — {'перенесён на ' + self._fmt(dt) if time_changed else 'восстановлен'} в расписании")
            E["matches"][key] = new
            return

        if self._owned(p, mid):
            changed = False
            if time_changed:
                logger.info(f"{head}: {label} — время изменено {self._fmt(old_dt)} → {self._fmt(dt)}, запись обновлена")
                p.match_time = dt
                p.notified_30 = False
                p.notified_5 = False
                p.date_basis = res.basis
                changed = True
            if text_changed:
                p.text = r.text
                p.crooked_notified = False   # пересчитать кривизну (как раньше при смене текста)
                self._tk_cache.pop(p.id, None)
                logger.info(f"{head}: {label} — текст прогноза изменён, запись обновлена")
                changed = True
            if changed:
                self.stats["updated"] += 1
        E["matches"][key] = new

    def _drop_owned_future(self, mid, rec, head, label, why):
        p = self.by_id.get(rec.get("pred_id")) if rec else None
        if self._owned(p, mid) and p.match_time > self.now:
            self._remove_pred(p)
            self.stats["removed"] += 1
            logger.info(f"{head}: {label} — убран из расписания ({why})")

    def _retract(self, mid, key, rec, head, why):
        label = f"«{(rec.get('text') or key)[:60]}»"
        p = self.by_id.get(rec.get("pred_id"))
        if rec.get("status") == "active" and p is not None:
            if self._owned(p, mid) and p.match_time > self.now:
                self._remove_pred(p)
                self.stats["removed"] += 1
                logger.info(f"{head}: {label} — {why}: убран из расписания "
                            f"(это не доказательство отмены матча, только отзыв прогноза)")
            else:
                logger.info(f"{head}: {label} — {why}, но запись остаётся "
                            f"(матч уже начался или запись не из этого сообщения)")
        rec["status"] = "removed"

    def _ensure_present(self, mid, E):
        """Сообщение не менялось, но будущий активный матч пропал из расписания (потерянная запись) — вернуть."""
        for key, rec in E["matches"].items():
            if rec.get("status") != "active" or rec.get("pred_id") in self.by_id:
                continue
            dt = _from_iso(rec.get("time"))
            if dt is None or dt <= self.now:
                continue   # сыгранные и вычищенные планировщиком обратно не возвращаем
            existing = self._find_existing(key, dt)
            if existing is not None:
                rec["pred_id"] = existing.id
                continue
            np_ = Prediction(text=rec.get("text") or key, match_time=dt, source="discord", msg_id=mid,
                             first_seen_at=rec.get("first_seen_at"), date_basis=rec.get("basis"))
            if rec.get("pred_id"):
                np_.id = rec["pred_id"]
            self._add_pred(np_)
            rec["pred_id"] = np_.id
            self.stats["restored"] += 1
            logger.info(f"Discord msg {mid}: «{np_.text[:60]}» — запись пропала из расписания, восстановлена на {self._fmt(dt)}")

    def retract_message(self, mid: str, why: str):
        E = self.ledger.get(mid)
        if not E:
            return
        head = f"Discord msg {mid}"
        for key, rec in E["matches"].items():
            if rec.get("status") != "removed":
                self._retract(mid, key, rec, head, why)

    def legacy_cleanup(self):
        """
        Записи прошлой версии бота (source=discord, без msg_id), которые ни одно сообщение
        не подтвердило датой, — это те самые «вчерашние матчи на сегодня». Не начавшиеся — убираем.
        """
        for p in list(self.preds):
            if p.source == "discord" and p.msg_id is None and p.match_time > self.now:
                self._remove_pred(p)
                self.stats["legacy_removed"] += 1
                self.stats["removed"] += 1
                logger.info(f"Discord legacy: «{p.text[:60]}» ({self._fmt(p.match_time)}) не подтверждена датой "
                            f"ни одного сообщения — убрана из расписания")

    def prune_ledger(self):
        limit = self.now - timedelta(hours=LEDGER_KEEP_HOURS)
        for mid in list(self.ledger):
            E = self.ledger[mid]
            last = _from_iso(E.get("edited_at")) or _from_iso(E.get("created_at"))
            if last and last < limit:
                del self.ledger[mid]


def apply_discord_messages(
    snapshots: list[dict],
    now: Optional[datetime] = None,
    tz_offset: Optional[int] = None,
    full_scan: bool = False,
    scan_floor: Optional[datetime] = None,
) -> dict:
    """
    Применяет снимки Discord-сообщений к общему расписанию.

    snapshot = {"id", "created_at", "edited_at" (или None), "content"}; времена — naive UTC.
    Работает одинаково для нового сообщения, правки и перечита истории: многократная
    обработка одной и той же версии ничего не меняет (идемпотентно), снимок старее
    уже обработанной версии игнорируется. Всё выполняется под одним локом поверх
    свежих данных Gist, поэтому параллельные события не создают дублей.

    full_scan=True (перечит истории канала) дополнительно:
      • убирает записи прошлой версии бота, которые не подтверждены ни одним сообщением;
      • отзывает матчи сообщений, удалённых из канала (если scan_floor покрывает их возраст);
      • чистит старые записи реестра.
    scan_floor — created_at самого старого полученного сообщения (datetime.min, если история
    прочитана целиком).

    Возвращает статистику; added_preds — только реально добавленные записи (для уведомления).
    """
    now = now or datetime.utcnow()
    tz = config.DEFAULT_TZ_OFFSET if tz_offset is None else tz_offset
    with _lock:
        preds = load_predictions()          # заодно прогоняет миграцию старого формата
        data = gist_storage.read(FILE_PREDS)
        ledger = data.get(LEDGER_KEY) or {}
        ledger_before = copy.deepcopy(ledger)
        shared_before = copy.deepcopy(data.get(SHARED_KEY, []))
        ps = _DiscordPass(preds, ledger, now, tz)

        for snap in snapshots:
            ps.process(snap)

        if full_scan:
            fetched = {str(s["id"]) for s in snapshots}
            floor = scan_floor or datetime.min
            horizon = now - timedelta(hours=match_dates.HISTORY_HORIZON_HOURS)
            for mid, E in list(ledger.items()):
                created = _from_iso(E.get("created_at"))
                if mid in fetched or created is None or created < floor or created < horizon:
                    continue
                if any(rec.get("status") != "removed" for rec in E["matches"].values()):
                    ps.retract_message(mid, "сообщение удалено из канала")
            ps.legacy_cleanup()
            ps.prune_ledger()

        shared_after = [p.to_dict() for p in sorted(ps.preds, key=lambda p: p.match_time)]
        if ps.ledger != ledger_before or shared_after != shared_before:   # в Gist пишем только реальные изменения
            data[SHARED_KEY] = shared_after
            data[LEDGER_KEY] = ps.ledger
            gist_storage.write(FILE_PREDS, data)

        stats = dict(ps.stats)
        stats["added_preds"] = ps.added_preds
        stats["undetermined_new"] = ps.undetermined_new
        stats["updated_count"] = stats["updated"]
        return stats


def retract_discord_message(message_id, now: Optional[datetime] = None) -> int:
    """Сообщение удалено из канала: отозвать его не начавшиеся матчи. Возвращает число убранных."""
    now = now or datetime.utcnow()
    with _lock:
        preds = load_predictions()
        data = gist_storage.read(FILE_PREDS)
        ledger = data.get(LEDGER_KEY) or {}
        ledger_before = copy.deepcopy(ledger)
        shared_before = copy.deepcopy(data.get(SHARED_KEY, []))
        ps = _DiscordPass(preds, ledger, now, config.DEFAULT_TZ_OFFSET)
        ps.retract_message(str(message_id), "сообщение удалено из канала")
        shared_after = [p.to_dict() for p in sorted(ps.preds, key=lambda p: p.match_time)]
        if ps.ledger != ledger_before or shared_after != shared_before:
            data[SHARED_KEY] = shared_after
            data[LEDGER_KEY] = ps.ledger
            gist_storage.write(FILE_PREDS, data)
        return ps.stats["removed"]



# ── Матчи с неопределённой датой: вопрос админу кнопками ─────────────────────

def ud_buttons(item: dict, now: Optional[datetime] = None, tz: Optional[int] = None) -> list[tuple[str, str]]:
    """Кнопки [(подпись, callback_data)] для одного неопределённого матча: сегодня / завтра / пропустить."""
    now = now or datetime.utcnow()
    tz = config.DEFAULT_TZ_OFFSET if tz is None else tz
    today = to_local(now, tz).date()
    tomorrow = today + timedelta(days=1)
    base = f"ud|{item['mid']}|{item['h8']}|"
    return [
        (f"📅 Сегодня {today:%d.%m}", base + today.strftime("%y%m%d")),
        (f"📅 Завтра {tomorrow:%d.%m}", base + tomorrow.strftime("%y%m%d")),
        ("🚫 Пропустить", base + "s"),
    ]


def list_undetermined(limit: int = 10) -> list[dict]:
    """Матчи, дату которых бот не смог определить и админ ещё не решил."""
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        ledger = data.get(LEDGER_KEY) or {}
    out = []
    for mid, E in ledger.items():
        for key, rec in (E.get("matches") or {}).items():
            if rec.get("status") == "undetermined" and not rec.get("dismissed"):
                out.append({"mid": mid, "key": key, "h8": _h8(key), "text": rec.get("text") or key,
                            "reason": rec.get("reason") or "", "hm": rec.get("hm") or "",
                            "created": _from_iso(E.get("created_at"))})
    out.sort(key=lambda x: x["created"] or datetime.min, reverse=True)
    return out[:limit]


def resolve_undetermined(mid: str, h8: str, choice: str, now: Optional[datetime] = None,
                         tz: Optional[int] = None) -> dict:
    """
    Решение админа по матчу с неопределённой датой. choice — YYMMDD (локальная дата матча)
    или 's' (пропустить). Возвращает {"status": added|linked|past|skipped|notfound, "text", "dt", "pred"}.
    Выбранная дата запоминается (rec.manual_date): правки сообщения её не перетирают.
    """
    now = now or datetime.utcnow()
    tz = config.DEFAULT_TZ_OFFSET if tz is None else tz
    with _lock:
        preds = load_predictions()
        data = gist_storage.read(FILE_PREDS)
        ledger = data.get(LEDGER_KEY) or {}
        ledger_before = copy.deepcopy(ledger)
        shared_before = copy.deepcopy(data.get(SHARED_KEY, []))
        ps = _DiscordPass(preds, ledger, now, tz)

        E = ledger.get(str(mid))
        found = None
        if E:
            for key, rec in E["matches"].items():
                if _h8(key) == h8 and rec.get("status") == "undetermined":
                    found = (key, rec)
                    break
        if not found:
            return {"status": "notfound", "text": "", "dt": None, "pred": None}
        key, rec = found
        text = rec.get("text") or key

        if choice == "s":
            rec["dismissed"] = True
            result = {"status": "skipped", "text": text, "dt": None, "pred": None}
        else:
            d = datetime.strptime(choice, "%y%m%d").date()
            hh, mm = (rec.get("hm") or "00:00").split(":")
            dt = local_to_utc(datetime.combine(d, dtime(int(hh), int(mm))), tz)
            if dt < ps.cutoff:
                return {"status": "past", "text": text, "dt": dt, "pred": None}
            existing = ps._find_existing(key, dt)
            if existing is not None:
                pred, status = existing, "linked"
            else:
                pred = Prediction(text=text, match_time=dt, source="discord", msg_id=str(mid),
                                  first_seen_at=_iso(now), date_basis="admin")
                ps._add_pred(pred)
                status = "added"
            rec.update(status="active", time=_iso(dt), manual_date=d.isoformat(), basis="admin",
                       pred_id=pred.id, dismissed=False)
            result = {"status": status, "text": text, "dt": dt, "pred": pred}

        shared_after = [p.to_dict() for p in sorted(ps.preds, key=lambda p: p.match_time)]
        if ledger != ledger_before or shared_after != shared_before:
            data[SHARED_KEY] = shared_after
            data[LEDGER_KEY] = ledger
            gist_storage.write(FILE_PREDS, data)
        return result


# ── Журнал Фонбета («нашёл / не нашёл и почему») ─────────────────────────────

JOURNAL_KEY = "fonbet_journal"
JOURNAL_MAX = 200


def journal_add(entry: dict, now: Optional[datetime] = None):
    """Добавляет запись в кольцевой журнал (последние JOURNAL_MAX). entry: kind=found|miss + детали."""
    entry = dict(entry, ts=_iso(now or datetime.utcnow()))
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        journal = list(data.get(JOURNAL_KEY) or [])
        journal.append(entry)
        data[JOURNAL_KEY] = journal[-JOURNAL_MAX:]
        gist_storage.write(FILE_PREDS, data)


def journal_recent(n: int = 10, kind: Optional[str] = None) -> list[dict]:
    """Последние записи журнала, новые первыми."""
    with _lock:
        journal = list(gist_storage.read(FILE_PREDS).get(JOURNAL_KEY) or [])
    if kind:
        journal = [e for e in journal if e.get("kind") == kind]
    return list(reversed(journal))[:n]


def journal_stats() -> dict:
    """Сводка по журналу: сколько найдено (по уровням) и сколько промахов (по причинам)."""
    with _lock:
        journal = list(gist_storage.read(FILE_PREDS).get(JOURNAL_KEY) or [])
    stats = {"total": len(journal), "found": 0, "miss": 0, "tiers": {}, "reasons": {}}
    for e in journal:
        if e.get("kind") == "found":
            stats["found"] += 1
            stats["tiers"][e.get("tier") or "?"] = stats["tiers"].get(e.get("tier") or "?", 0) + 1
        elif e.get("kind") == "miss":
            stats["miss"] += 1
            stats["reasons"][e.get("reason") or "?"] = stats["reasons"].get(e.get("reason") or "?", 0) + 1
    return stats


# ── Алиасы названий команд (подтверждённые пары прогноз → Фонбет) ────────────

ALIASES_KEY = "fonbet_aliases"
_alias_cache: dict = {"ts": 0.0, "data": None}
_ALIAS_TTL_SEC = 60


def load_aliases() -> dict:
    """{alias_norm(название из прогноза): [alias_norm(название на Фонбете), ...]} — для find_matching_event."""
    import time as _t
    with _lock:
        if _alias_cache["data"] is not None and _t.time() - _alias_cache["ts"] < _ALIAS_TTL_SEC:
            return _alias_cache["data"]
        raw = gist_storage.read(FILE_PREDS).get(ALIASES_KEY) or {}
        out = {k: list((v.get("evs") or {}).keys()) for k, v in raw.items()}
        _alias_cache.update(ts=_t.time(), data=out)
        return out


def add_aliases(pairs: list, now: Optional[datetime] = None) -> int:
    """Запоминает пары (название из прогноза, название на Фонбете). Возвращает число новых/обновлённых."""
    from fonbet import alias_norm
    if not pairs:
        return 0
    changed = 0
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        aliases = data.get(ALIASES_KEY) or {}
        for pred_name, ev_name in pairs:
            pk, ek = alias_norm(pred_name), alias_norm(ev_name)
            if not pk or not ek or pk == ek:
                continue
            entry = aliases.setdefault(pk, {"pred": pred_name.strip(), "evs": {}, "n": 0})
            entry["evs"][ek] = ev_name.strip()
            entry["n"] = entry.get("n", 0) + 1
            entry["ts"] = _iso(now or datetime.utcnow())
            changed += 1
        if changed:
            data[ALIASES_KEY] = aliases
            gist_storage.write(FILE_PREDS, data)
            _alias_cache["data"] = None
    return changed


def list_aliases(limit: int = 40) -> list[dict]:
    with _lock:
        raw = gist_storage.read(FILE_PREDS).get(ALIASES_KEY) or {}
    rows = [{"key": k, "pred": v.get("pred", k), "evs": list((v.get("evs") or {}).values()),
             "n": v.get("n", 1), "ts": v.get("ts")} for k, v in raw.items()]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows[:limit]


def remove_alias(name: str) -> bool:
    """Удаляет алиас по названию из прогноза (любой регистр/знаки). True если был."""
    from fonbet import alias_norm
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        aliases = data.get(ALIASES_KEY) or {}
        if alias_norm(name) not in aliases:
            return False
        del aliases[alias_norm(name)]
        data[ALIASES_KEY] = aliases
        gist_storage.write(FILE_PREDS, data)
        _alias_cache["data"] = None
        return True


def update_prediction(chat_id: int = None, pred_id: str = None, **kwargs):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        for p in preds:
            if p.id == pred_id:
                for k, v in kwargs.items():
                    setattr(p, k, v)
        data[SHARED_KEY] = [p.to_dict() for p in preds]
        gist_storage.write(FILE_PREDS, data)


def delete_prediction(chat_id: int = None, pred_id: str = None) -> bool:
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        new_preds = [p for p in preds if p.id != pred_id]
        if len(new_preds) == len(preds):
            return False
        data[SHARED_KEY] = [p.to_dict() for p in new_preds]
        gist_storage.write(FILE_PREDS, data)
        return True


def clear_predictions(chat_id: int = None):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        data[SHARED_KEY] = []
        gist_storage.write(FILE_PREDS, data)


def get_all_recipient_chat_ids() -> list[int]:
    """
    Возвращает chat_id ВСЕХ авторизованных не-забаненных пользователей —
    им нужно слать уведомления о прогнозах.
    """
    import auth
    users = auth.list_users()
    chat_ids = [u.user_id for u in users if not u.banned]
    # Админы тоже получают уведомления, даже если их нет в whitelist
    for admin_id in config.ADMIN_CHAT_IDS:
        if admin_id not in chat_ids:
            chat_ids.append(admin_id)
    return chat_ids


# Совместимость со старым кодом
def get_all_chat_ids() -> list[int]:
    return get_all_recipient_chat_ids()


# ── Settings (индивидуальные у каждого) ──────────────────────────────────────

def load_settings(chat_id: int) -> UserSettings:
    data = gist_storage.read(FILE_SETTINGS)
    raw = data.get(str(chat_id))
    if raw:
        return UserSettings.from_dict(raw)
    return UserSettings(chat_id=chat_id, timezone_offset=config.DEFAULT_TZ_OFFSET)


def save_settings(settings: UserSettings):
    with _lock:
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE_SETTINGS)
        data[str(settings.chat_id)] = settings.to_dict()
        gist_storage.write(FILE_SETTINGS, data)


def toggle_setting(user_id: int, field: str) -> UserSettings:
    """
    Атомарно переключает булево поле настроек.
    Читает СВЕЖИЕ данные (минуя кэш), меняет одно поле, сохраняет.
    Защищает от затирания при быстрых нажатиях тумблеров.
    Возвращает обновлённые настройки.
    """
    with _lock:
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE_SETTINGS)
        raw = data.get(str(user_id))
        if raw:
            s = UserSettings.from_dict(raw)
        else:
            s = UserSettings(chat_id=user_id, timezone_offset=config.DEFAULT_TZ_OFFSET)

        current = getattr(s, field, True)
        setattr(s, field, not current)

        data[str(user_id)] = s.to_dict()
        gist_storage.write(FILE_SETTINGS, data)
        return s


def update_settings(user_id: int, mutate) -> UserSettings:
    """
    Атомарно меняет настройки пользователя: свежее чтение → mutate(settings) → запись.
    Нужно для списков (muted_ids, blocked_countries), которые toggle_setting не умеет.
    """
    with _lock:
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE_SETTINGS)
        raw = data.get(str(user_id))
        s = UserSettings.from_dict(raw) if raw else UserSettings(
            chat_id=user_id, timezone_offset=config.DEFAULT_TZ_OFFSET)
        mutate(s)
        data[str(user_id)] = s.to_dict()
        gist_storage.write(FILE_SETTINGS, data)
        return s
