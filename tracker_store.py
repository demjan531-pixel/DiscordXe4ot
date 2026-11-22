"""Хранилище задач без зависимостей от Discord."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import re
import sqlite3

STATUSES = {
    "open": "К выполнению",
    "doing": "В работе",
    "review": "На проверке",
    "blocked": "Заблокировано",
    "done": "Готово",
}
PRIORITIES = {
    "low": "Низкий",
    "normal": "Обычный",
    "high": "Высокий",
    "urgent": "Срочный",
}
AREAS = {
    "gameplay": "Механики",
    "maps": "Карты",
    "ui": "Интерфейс",
    "art": "Арт и звук",
    "qa": "Тестирование",
    "other": "Другое",
}


class InputError(ValueError):
    pass


def clean(value, label, limit, required=False):
    value = " ".join((value or "").split())
    if len(value) > limit:
        raise InputError(f"{label}: максимум {limit} символов.")
    if required and not value:
        raise InputError(f"{label}: нельзя оставить пустым.")
    return value


def deadline(value):
    if value is None:
        return None
    try:
        value = value.strip()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value).isoformat()
        if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
            day, month, year = map(int, value.split("."))
            return date(year, month, day).isoformat()
    except ValueError:
        pass
    raise InputError("Неверная дата. Укажи ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL,
                title TEXT NOT NULL, area TEXT NOT NULL, description TEXT NOT NULL,
                creator TEXT NOT NULL, assignee TEXT, due TEXT,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','doing','review','blocked','done')),
                reason TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS task_board
                          ON tasks (guild, status, due, id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS summary_log (
                          guild TEXT NOT NULL, day TEXT NOT NULL,
                          PRIMARY KEY (guild, day))""")

            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "team_role" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN team_role TEXT")
            if "priority" not in columns:
                db.execute("""ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL
                              DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent'))""")
            if "deleted_at" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
            db.execute("""CREATE INDEX IF NOT EXISTS task_teams
                          ON tasks(guild, team_role, status, id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL,
                task_id INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS task_history_lookup
                          ON task_history(guild, task_id, id)""")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, guild, creator, title, area, description=None, assignee=None, due=None,
               *, team_role=None, priority="normal"):
        title = clean(title, "Название", 120, required=True)
        description = clean(description, "Описание", 600)
        due = deadline(due)
        if area not in AREAS:
            raise InputError("Выбери направление из списка.")
        if priority not in PRIORITIES:
            raise InputError("Выбери приоритет из списка.")
        with self.db() as db:
            cursor = db.execute("""INSERT INTO tasks
                (guild, creator, title, area, description, assignee, due, team_role, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(guild), str(creator), title, area, description,
                 str(assignee) if assignee is not None else None, due,
                 str(team_role) if team_role is not None else None, priority))
            self._log(db, guild, cursor.lastrowid, creator, "create", "Задача создана.")
            return self._get(db, guild, cursor.lastrowid)

    @staticmethod
    def _log(db, guild, task_id, actor, action, detail):
        db.execute("""INSERT INTO task_history (guild, task_id, actor, action, detail)
                      VALUES (?, ?, ?, ?, ?)""", (str(guild), task_id, str(actor), action, detail))

    def history(self, guild, task_id, limit=8):
        with self.db() as db:
            exists = db.execute("SELECT 1 FROM tasks WHERE guild = ? AND id = ?",
                                (str(guild), task_id)).fetchone()
            if exists is None:
                raise InputError(f"Задача #{task_id} не найдена.")
            return db.execute("""SELECT actor, action, detail, created_at FROM task_history
                WHERE guild = ? AND task_id = ? ORDER BY id DESC LIMIT ?""",
                (str(guild), task_id, max(1, min(25, limit)))).fetchall()

    def _get(self, db, guild, task_id):
        row = db.execute("SELECT * FROM tasks WHERE guild = ? AND id = ? AND deleted_at IS NULL",
                         (str(guild), task_id)).fetchone()
        if row is None:
            raise InputError(f"Задача #{task_id} не найдена.")
        return row

    def get(self, guild, task_id):
        with self.db() as db:
            return self._get(db, guild, task_id)

    def _editable(self, db, guild, task_id, actor, manager, actor_roles=()):
        # Serialize authorization and change even if the DB is opened twice.
        db.execute("BEGIN IMMEDIATE")
        row = self._get(db, guild, task_id)
        if not manager and str(actor) != row["creator"]:
            if str(actor) != row["assignee"]:
                raise InputError("Менять задачу может её автор, ответственный или руководитель.")
            if row["team_role"] and row["team_role"] not in {str(role) for role in actor_roles}:
                raise InputError("У ответственного больше нет роли команды. Обратись к руководителю.")
        return row

    def set_status(self, guild, task_id, actor, manager, status, reason=None, *, actor_roles=()):
        if status not in STATUSES:
            raise InputError("Выбери статус из списка.")
        if status != "blocked" and reason:
            raise InputError("Причина указывается только для статуса «Заблокировано».")
        reason = clean(reason, "Причина", 240, required=status == "blocked")
        with self.db() as db:
            previous = self._editable(db, guild, task_id, actor, manager, actor_roles)
            if previous["status"] == status and previous["reason"] == reason:
                return previous
            db.execute("""UPDATE tasks SET status = ?, reason = ?, changed_at = CURRENT_TIMESTAMP
                          WHERE guild = ? AND id = ?""",
                       (status, reason, str(guild), task_id))
            detail = f"Статус: {STATUSES[previous['status']]} → {STATUSES[status]}."
            if reason:
                detail += f" Причина: {reason}"
            self._log(db, guild, task_id, actor, "status", detail)
            return self._get(db, guild, task_id)

    def edit(self, guild, task_id, actor, manager, *, title=None, area=None, description=None,
             assignee=None, due=None, clear_assignee=False, clear_due=False,
             team_role=None, clear_team_role=False, priority=None, actor_roles=()):
        if clear_team_role and team_role is not None:
            raise InputError("Нельзя одновременно указать и убрать роль команды.")
        if clear_assignee and assignee is not None:
            raise InputError("Нельзя одновременно указать и убрать ответственного.")
        if clear_due and due is not None:
            raise InputError("Нельзя одновременно указать и убрать срок.")
        fields = {}
        if title is not None:
            fields["title"] = clean(title, "Название", 120, required=True)
        if area is not None:
            if area not in AREAS:
                raise InputError("Выбери направление из списка.")
            fields["area"] = area
        if description is not None:
            fields["description"] = clean(description, "Описание", 600)
        if assignee is not None or clear_assignee:
            fields["assignee"] = None if clear_assignee else str(assignee)
        if due is not None or clear_due:
            fields["due"] = None if clear_due else deadline(due)
        if team_role is not None or clear_team_role:
            fields["team_role"] = None if clear_team_role else str(team_role)
        if priority is not None:
            if priority not in PRIORITIES:
                raise InputError("Выбери приоритет из списка.")
            fields["priority"] = priority
        if not fields:
            raise InputError("Укажи хотя бы одно изменение.")
        with self.db() as db:
            previous = self._editable(db, guild, task_id, actor, manager, actor_roles)
            if "team_role" in fields and previous["team_role"] != fields["team_role"] and not (
                    manager or str(actor) == previous["creator"]):
                raise InputError("Переназначить роль может автор или руководитель.")
            if ("assignee" in fields and previous["assignee"] != fields["assignee"]
                    and fields["assignee"] is not None and not (
                        manager or str(actor) == previous["creator"])):
                raise InputError("Назначить другого ответственного может автор или руководитель.")
            fields = {k: v for k, v in fields.items() if previous[k] != v}
            if not fields:
                return previous
            # Names are fixed above; only values are user-supplied.
            assignments = ", ".join(f"{name} = ?" for name in fields)
            db.execute(f"UPDATE tasks SET {assignments}, changed_at = CURRENT_TIMESTAMP "
                       "WHERE guild = ? AND id = ?",
                       (*fields.values(), str(guild), task_id))
            labels = {"title": "название", "area": "направление", "description": "описание",
                      "assignee": "ответственный", "due": "срок", "team_role": "роль",
                      "priority": "приоритет"}
            self._log(db, guild, task_id, actor, "edit",
                      "Изменено: " + ", ".join(labels[k] for k in fields) + ".")
            return self._get(db, guild, task_id)

    def delete(self, guild, task_id, actor, manager):
        """Soft-delete a task so its history and ID remain available for audit."""
        with self.db() as db:
            previous = self._editable(db, guild, task_id, actor, manager)
            db.execute("UPDATE tasks SET deleted_at = CURRENT_TIMESTAMP, changed_at = CURRENT_TIMESTAMP "
                       "WHERE guild = ? AND id = ?", (str(guild), task_id))
            self._log(db, guild, task_id, actor, "delete",
                      f"Задача удалена: {previous['title'][:100]}.")
            return previous

    def claim(self, guild, task_id, actor, actor_roles):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, guild, task_id)
            if not row["team_role"] or row["team_role"] not in {str(role) for role in actor_roles}:
                raise InputError("Взять эту задачу может только участник назначенной роли.")
            if row["assignee"] is not None:
                raise InputError("У задачи уже есть ответственный.")
            if row["status"] not in ("open", "doing"):
                raise InputError("Взять можно только задачу «К выполнению» или «В работе».")
            db.execute("""UPDATE tasks SET assignee = ?, status = 'doing', reason = '',
                          changed_at = CURRENT_TIMESTAMP WHERE guild = ? AND id = ?""",
                       (str(actor), str(guild), task_id))
            self._log(db, guild, task_id, actor, "claim", "Участник взял задачу в работу.")
            return self._get(db, guild, task_id)

    def board(self, guild, status=None, area=None, assignee=None, page=1, size=5, *,
              team_role=None, priority=None):
        if page < 1 or size < 1:
            raise InputError("Номер страницы должен быть не меньше 1.")
        conditions, args = ["guild = ?", "deleted_at IS NULL"], [str(guild)]
        # Map fixed SQL identifiers to runtime values.
        for key, value in [("status", status), ("area", area), ("assignee", assignee),
                           ("team_role", team_role), ("priority", priority)]:
            if value is not None:
                conditions.append(f"{key} = ?")
                args.append(str(value))
        where = " AND ".join(conditions)
        with self.db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"""SELECT * FROM tasks WHERE {where}
                ORDER BY (status = 'done'),
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                  WHEN 'normal' THEN 2 ELSE 3 END,
                    (due IS NULL), due, id
                LIMIT ? OFFSET ?""", (*args, size, (page - 1) * size)).fetchall()
            return rows, total

    @staticmethod
    def _scope(guild, area, team_role):
        conditions, args = ["guild = ?", "deleted_at IS NULL"], [str(guild)]
        for key, value in [("area", area), ("team_role", team_role)]:
            if value is not None:
                conditions.append(f"{key} = ?")
                args.append(str(value))
        return " AND ".join(conditions), args

    def progress(self, guild, today, *, area=None, team_role=None):
        where, scope = self._scope(guild, area, team_role)
        mark = today.isoformat()
        with self.db() as db:
            counts = db.execute(f"""SELECT area, status, COUNT(*) AS n FROM tasks
                          WHERE {where} GROUP BY area, status""", scope).fetchall()
            overdue = db.execute(f"""SELECT COUNT(*) FROM tasks
                          WHERE {where} AND status != 'done' AND due < ?""",
                          (*scope, mark)).fetchone()[0]
            blocked = db.execute(f"""SELECT * FROM tasks WHERE {where} AND status = 'blocked'
                          ORDER BY (due IS NULL), due, id LIMIT 2""", scope).fetchall()
            return counts, overdue, blocked

    def due_tasks(self, guild, today, *, area=None, team_role=None):
        where, scope = self._scope(guild, area, team_role)
        with self.db() as db:
            return db.execute(f"""SELECT * FROM tasks
                          WHERE {where} AND status != 'done' AND due <= ?
                          ORDER BY due, id LIMIT 3""",
                          (*scope, today.isoformat())).fetchall()

    def summary_sent(self, guild, day):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM summary_log WHERE guild = ? AND day = ?",
                                   (str(guild), day.isoformat())).fetchone())

    def mark_summary_sent(self, guild, day):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO summary_log VALUES (?, ?)",
                       (str(guild), day.isoformat()))
