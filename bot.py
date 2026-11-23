"""Discord-бот для задач проекта Invokyr → Roblox."""
import logging
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from tracker_store import AREAS, PRIORITIES, STATUSES, InputError, Store

LOG = logging.getLogger("invokyr")
# Discord sends restricted channels to the Gateway with incomplete overwrites.
# https://docs.discord.com/developers/resources/channel#obfuscated-channels
CHANNEL_OBFUSCATED = 1 << 17


def read_id(name, required=False):
    raw = os.getenv(name, "").strip()
    if not raw and not required:
        return None
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20 or not 0 < int(raw) < (1 << 64):
        raise SystemExit(f"Укажи корректный числовой ID в {name}.")
    return int(raw)


def read_role_ids(name):
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part.isascii() or not part.isdecimal() or len(part) > 20 or not 0 < int(part) < (1 << 64):
            raise SystemExit(f"Укажи ID ролей в {name} через запятую.")
        ids.add(int(part))
        if len(ids) > 40:
            raise SystemExit(f"Укажи до 40 ролей в {name}.")
    return frozenset(ids)


def actor_roles(interaction):
    member = interaction.user
    return tuple(role.id for role in member.roles) if isinstance(member, discord.Member) else ()


def can_verify_channel_access(channel, bot_member):
    try:
        flags = getattr(channel, "flags", None)
        if flags is not None and flags.value & CHANNEL_OBFUSCATED:
            return False
        # Older SDK versions do not expose flags on every channel class.
        # Do not assume a missing flag implies complete metadata.
        return channel.permissions_for(bot_member).view_channel is True
    except (AttributeError, TypeError, ValueError):
        return False


def check_task_assignment(interaction, team_role, assignee):
    if assignee is not None and (assignee.bot or assignee.guild.id != interaction.guild_id):
        raise InputError("Выбери участника этого сервера, а не бота.")
    if team_role is not None:
        if team_role.guild.id != interaction.guild_id or team_role.is_default() or team_role.managed:
            raise InputError("Для задачи выбери обычную роль команды на этом сервере.")
        if assignee is not None and assignee.get_role(team_role.id) is None:
            raise InputError("Ответственный должен состоять в указанной роли задачи.")


def assignment_label(row):
    person = f"Исполнитель: <@{row['assignee']}>" if row["assignee"] else ""
    role = f"Роль: <@&{row['team_role']}>" if row["team_role"] else ""
    return " · ".join(filter(None, (person, role))) or "без роли и исполнителя"


def safe(value, limit=None):
    if limit and len(value) > limit:
        value = value[:limit - 1] + "…"
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def task_line(row, today):
    overdue = row["status"] != "done" and row["due"] and row["due"] < today.isoformat()
    assignee = assignment_label(row)
    return (
        f"**#{row['id']}** {safe(row['title'], 82)} · {STATUSES[row['status']]}\n"
        f"   {AREAS[row['area']]} · {PRIORITIES[row['priority']]} · {assignee} · {row['due'] or 'без срока'}"
        f"{' · ⚠️ просрочено' if overdue else ''}"
    )


def task_detail(row, today):
    result = task_line(row, today)
    if len(row["title"]) > 82:
        result += f"\nПолное название: {safe(row['title'])}"
    if row["description"]:
        result += f"\nОписание: {safe(row['description'], 440)}"
    if row["reason"]:
        result += f"\nПричина блокировки: {safe(row['reason'], 220)}"
    return result


TASK_PAGE_SIZE = 5


def task_list_page(rows, total, page, today):
    if not total:
        return "С такими фильтрами задач нет."
    pages = math.ceil(total / TASK_PAGE_SIZE)
    header = f"**Задачи · страница {page}/{pages} · всего {total}**"
    return "\n".join([header, *(task_line(row, today) for row in rows)])[:2000]


class TaskListView(discord.ui.View):
    def __init__(self, store, guild, filters, page, total, today):
        super().__init__(timeout=900)
        self.store = store
        self.guild = guild
        self.filters = filters
        self.page = page
        self.pages = max(1, math.ceil(total / TASK_PAGE_SIZE))
        self.today = today
        self.owner_id = None
        self.previous_button.disabled = page <= 1
        self.next_button.disabled = page >= self.pages

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Листать этот список может только тот, кто его открыл.", ephemeral=True)
            return False
        return True

    async def _show_page(self, interaction, page):
        await interaction.response.defer()
        rows, total = self.store.board(
            self.guild, page=page, size=TASK_PAGE_SIZE, **self.filters)
        self.pages = max(1, math.ceil(total / TASK_PAGE_SIZE))
        self.page = min(max(1, page), self.pages)
        if self.page != page and total:
            rows, total = self.store.board(
                self.guild, page=self.page, size=TASK_PAGE_SIZE, **self.filters)
        self.previous_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.pages
        await interaction.edit_original_response(
            content=task_list_page(rows, total, self.page, self.today), view=self)

    @discord.ui.button(label="← Назад", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction, button):
        await self._show_page(interaction, self.page - 1)

    @discord.ui.button(label="Дальше →", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction, button):
        await self._show_page(interaction, self.page + 1)


def summary(store, guild, today, *, area=None, team_role=None):
    rows, overdue, blocked = store.progress(guild, today, area=area, team_role=team_role)
    scope = " · ".join(filter(None, (AREAS.get(area), f"<@&{team_role}>" if team_role else "")))
    if not rows:
        if scope:
            return f"**Invokyr → Roblox · {scope}**\nС такими фильтрами задач нет."
        return "**Invokyr → Roblox**\nПока нет задач. Добавь первую через /task create."
    counts = dict.fromkeys(STATUSES, 0)
    by_area = {}
    for row in rows:
        area, status, n = row["area"], row["status"], row["n"]
        counts[status] += n
        by_area.setdefault(area, dict.fromkeys(STATUSES, 0))[status] += n
    total = sum(counts.values())
    lines = [
        f"**Invokyr → Roblox · {today:%d.%m.%Y}**" + (f" · {scope}" if scope else ""),
        f"Закрыто задач: **{counts['done']}/{total} ({counts['done'] * 100 / total:.0f}%)**",
        " · ".join(f"{label}: {counts[key]}" for key, label in STATUSES.items()),
        f"Просрочено: **{overdue}**",
        "",
        "**Готово по направлениям**",
        *[f"• {AREAS[key]}: {val['done']}/{sum(val.values())}" for key, val in by_area.items()],
    ]
    due = store.due_tasks(guild, today, area=area, team_role=team_role)
    if due:
        lines += ["", "**Срок сегодня или уже прошёл**"]
        for row in due:
            assignee = assignment_label(row)
            lines.append(f"• #{row['id']} {safe(row['title'], 60)} · {assignee} · {row['due']}")
        if overdue > len(due):
            lines.append("Другие сроки видны через /task list.")
    if blocked:
        lines += ["", "**Что блокирует работу**"]
        for row in blocked:
            lines.append(f"• #{row['id']} {safe(row['title'], 65)}: {safe(row['reason'], 105)}")
        if counts["blocked"] > len(blocked):
            lines.append("Остальные: /task list → статус «Заблокировано».")
    return "\n".join(lines)[:1950]


class Tracker(discord.Client):
    def __init__(self, guild_id, store, tz, team_role, manager_role, project_channel,
                 summary_enabled, summary_time, role_manage_ids=()):
        # Keep the non-privileged role cache, but no message or member intents.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild = discord.Object(id=guild_id)
        self.store = store
        self.tz = tz
        self.team_role = team_role
        self.manager_role = manager_role
        self.role_manage_ids = frozenset(role_manage_ids)
        self.project_channel = project_channel
        self.summary_enabled = summary_enabled
        self.summary_time = summary_time
        self.tree = TrackerTree(self)
        self.tree.add_command(build_group(self), guild=self.guild)
        self.tree.add_command(build_role_group(self), guild=self.guild)
        self.tree.add_command(build_progress(self), guild=self.guild)

    async def setup_hook(self):
        commands = await self.tree.sync(guild=self.guild)
        LOG.info("Команды зарегистрированы: %s", ", ".join(cmd.name for cmd in commands))
        if self.summary_enabled:
            self.daily_summary.start()

    async def on_ready(self):
        LOG.info("Бот подключён: %s", self.user)

    async def close(self):
        if self.daily_summary.is_running():
            self.daily_summary.cancel()
        await super().close()

    def today(self):
        return datetime.now(self.tz).date()

    def is_manager(self, interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return interaction.permissions.manage_guild or (
            self.manager_role is not None and member.get_role(self.manager_role) is not None
        )

    def is_role_operator(self, interaction):
        guild = interaction.guild
        actor = interaction.user
        if guild is None or not isinstance(actor, discord.Member):
            return False
        return actor.id == guild.owner_id or (
            actor.guild_permissions.manage_roles and self.manager_role is not None
            and actor.get_role(self.manager_role) is not None
        )

    def role_operator(self, interaction):
        if not self.role_manage_ids:
            raise InputError("Выдача ролей выключена. Владелец должен задать ROLE_MANAGE_IDS.")
        if interaction.guild_id != self.guild.id or (
            self.project_channel is not None and interaction.channel_id != self.project_channel
        ):
            raise InputError("Роли проекта можно менять только в настроенном канале сервера.")
        if not self.is_role_operator(interaction):
            raise InputError("Нужен владелец сервера либо роль руководителя и право «Управлять ролями».")
        guild = interaction.guild
        # A populated guild and members/roles cache are required to check hierarchy.
        if guild.unavailable or guild.me is None or not guild.channels:
            raise InputError("Discord ещё не загрузил роли и каналы сервера. Попробуй позже.")
        return guild, interaction.user

    def check_role_change(self, interaction, member, role):
        guild, actor = self.role_operator(interaction)
        me = guild.me
        if role.guild.id != guild.id or role.id not in self.role_manage_ids:
            raise InputError("Эта роль не разрешена в ROLE_MANAGE_IDS.")
        if role.is_default() or role.managed or role.id == self.manager_role:
            raise InputError("Нельзя менять системную роль, @everyone или роль руководителя.")
        if any(not can_verify_channel_access(channel, me) for channel in guild.channels):
            raise InputError("Бот не может проверить доступ во всех каналах. Измени роль вручную в Discord.")
        if role.permissions.value != 0 or any(
            bits.value != 0 for channel in guild.channels for bits in channel.overwrites_for(role).pair()
        ):
            # A deny-only override may be a quarantine: removal could expand access.
            raise InputError("Можно менять только роли без прав и без настроек доступа в каналах.")
        if member.guild.id != guild.id or member.bot or member.id in (guild.owner_id, actor.id):
            raise InputError("Нельзя менять роли себе, владельцу сервера или боту.")
        if not me.guild_permissions.manage_roles or me.guild_permissions.administrator:
            raise InputError("Боту нужно право «Управлять ролями», но не «Администратор».")
        if role >= me.top_role or member.top_role >= me.top_role:
            raise InputError("Роль и участник должны быть ниже роли бота.")
        if actor.id != guild.owner_id and (role >= actor.top_role or member.top_role >= actor.top_role):
            raise InputError("Роль и участник должны быть ниже твоей высшей роли.")

    @tasks.loop(minutes=1)
    async def daily_summary(self):
        if datetime.now(self.tz).time() < self.summary_time:
            return
        day = self.today()
        if self.store.summary_sent(self.guild.id, day):
            return
        try:
            channel = await self.fetch_channel(self.project_channel)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild.id:
                LOG.error("PROJECT_CHANNEL_ID должен указывать на текстовый канал этого сервера.")
                self.daily_summary.stop()
                return
            await channel.send(summary(self.store, self.guild.id, day), suppress_embeds=True)
            self.store.mark_summary_sent(self.guild.id, day)
        except (discord.Forbidden, discord.NotFound):
            LOG.exception("Не могу отправить сводку. Проверь канал и права, затем перезапусти бота.")
            self.daily_summary.stop()
        except discord.HTTPException:
            LOG.exception("Discord не принял сводку; повторю попытку.")

    @daily_summary.before_loop
    async def before_summary(self):
        await self.wait_until_ready()


class TrackerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        bot = self.client
        if interaction.guild_id != bot.guild.id:
            await interaction.response.send_message(
                "Этот бот настроен для другого сервера.", ephemeral=True)
            return False
        if bot.project_channel is not None and interaction.channel_id != bot.project_channel:
            await interaction.response.send_message(
                f"Команды проекта доступны в <#{bot.project_channel}>.", ephemeral=True)
            return False
        member = getattr(interaction, "user", None)
        # Role operations have their own stricter checks, including for a manager
        # who may not yet have the ordinary project role.
        payload = getattr(interaction, "data", None) or {}
        is_role_command = payload.get("name") == "role"
        if bot.team_role is not None and not is_role_command and not bot.is_manager(interaction) and (
            not isinstance(member, discord.Member) or member.get_role(bot.team_role) is None
        ):
            await interaction.response.send_message(
                "Команда доступна только проектной роли.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error):
        source = getattr(error, "original", error)
        if isinstance(source, InputError):
            message = str(source)
        else:
            LOG.error("Сбой команды", exc_info=(type(source), source, source.__traceback__))
            message = "Команду не удалось выполнить. Попробуй ещё раз или сообщи владельцу бота."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException as reply_error:
            LOG.warning("Не удалось отправить ответ об ошибке: %s", reply_error)


STATUS_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in STATUSES.items()]
AREA_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in AREAS.items()]
PRIORITY_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in PRIORITIES.items()]


def build_progress(bot):
    @app_commands.command(name="progress", description="Сводка прогресса проекта Invokyr → Roblox")
    @app_commands.describe(area="Только одно направление", team_role="Только задачи выбранной роли")
    @app_commands.choices(area=AREA_CHOICES)
    async def progress(interaction: discord.Interaction, area: str | None = None,
                       team_role: discord.Role | None = None):
        result = summary(bot.store, bot.guild.id, bot.today(), area=area,
                         team_role=team_role.id if team_role else None)
        await interaction.response.send_message(result, ephemeral=True, suppress_embeds=True)
    return progress


def build_role_group(bot):
    group = app_commands.Group(name="role", description="Выдача разрешённых ролей проекта")

    @group.command(name="grant", description="Выдать участнику разрешённую роль проекта")
    @app_commands.describe(member="Кому выдать роль", role="Какая роль из разрешённого списка")
    async def grant(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        bot.check_role_change(interaction, member, role)
        if member.get_role(role.id) is not None:
            await interaction.response.send_message("У участника уже есть эта роль.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Invokyr Tracker: grant by {interaction.user.id}")
        except discord.Forbidden:
            raise InputError("Discord отказал. Проверь право «Управлять ролями» и порядок ролей.") from None
        LOG.info("Role grant guild=%s by=%s member=%s role=%s",
                 interaction.guild_id, interaction.user.id, member.id, role.id)
        await interaction.response.send_message(
            f"Роль <@&{role.id}> выдана участнику <@{member.id}>.", ephemeral=True)

    @group.command(name="remove", description="Снять с участника разрешённую роль проекта")
    @app_commands.describe(member="С кого снять роль", role="Какая роль из разрешённого списка")
    async def remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        bot.check_role_change(interaction, member, role)
        if member.get_role(role.id) is None:
            await interaction.response.send_message("У участника нет этой роли.", ephemeral=True)
            return
        try:
            await member.remove_roles(role, reason=f"Invokyr Tracker: remove by {interaction.user.id}")
        except discord.Forbidden:
            raise InputError("Discord отказал. Проверь право «Управлять ролями» и порядок ролей.") from None
        LOG.info("Role remove guild=%s by=%s member=%s role=%s",
                 interaction.guild_id, interaction.user.id, member.id, role.id)
        await interaction.response.send_message(
            f"Роль <@&{role.id}> снята с участника <@{member.id}>.", ephemeral=True)

    @group.command(name="list", description="Показать роли из настроенного списка")
    async def role_list(interaction: discord.Interaction):
        guild, _ = bot.role_operator(interaction)
        found = [f"<@&{rid}>" if guild.get_role(rid) else f"удалённая роль {rid}"
                 for rid in sorted(bot.role_manage_ids)]
        await interaction.response.send_message(
            "Роли из ROLE_MANAGE_IDS: " + ", ".join(found), ephemeral=True)
    return group


def build_group(bot):
    group = app_commands.Group(name="task", description="Задачи проекта Invokyr → Roblox")
    guild = bot.guild.id

    @group.command(name="create", description="Добавить задачу в проект")
    @app_commands.describe(title="Короткое название", area="Направление проекта",
                           assignee="Ответственный участник", due="Срок: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ",
                           details="Описание задачи", team_role="Роль, которая может взять задачу",
                           priority="Приоритет задачи")
    @app_commands.choices(area=AREA_CHOICES, priority=PRIORITY_CHOICES)
    async def create(interaction: discord.Interaction, title: str, area: str,
                     assignee: discord.Member | None = None, due: str | None = None,
                     details: str | None = None, team_role: discord.Role | None = None,
                     priority: str = "normal"):
        check_task_assignment(interaction, team_role, assignee)
        row = bot.store.create(guild, interaction.user.id, title, area, details,
                               assignee.id if assignee else None, due,
                               team_role=team_role.id if team_role else None, priority=priority)
        await interaction.response.send_message("Добавлено:\n" + task_detail(row, bot.today()),
                                                ephemeral=True, suppress_embeds=True)

    @group.command(name="list", description="Показать задачи, при желании с фильтром")
    @app_commands.describe(status="Фильтр по статусу", area="Фильтр по направлению",
                           assignee="Фильтр по ответственному", page="Начальная страница списка",
                           team_role="Фильтр по роли", priority="Фильтр по приоритету")
    @app_commands.choices(status=STATUS_CHOICES, area=AREA_CHOICES, priority=PRIORITY_CHOICES)
    async def list_tasks(interaction: discord.Interaction, status: str | None = None,
                         area: str | None = None, assignee: discord.Member | None = None,
                         page: app_commands.Range[int, 1, 10000] = 1,
                         team_role: discord.Role | None = None, priority: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        filters = {
            "status": status,
            "area": area,
            "assignee": assignee.id if assignee else None,
            "team_role": team_role.id if team_role else None,
            "priority": priority,
        }
        rows, total = bot.store.board(
            guild, page=page, size=TASK_PAGE_SIZE, **filters)
        if not total:
            await interaction.edit_original_response(
                content=task_list_page(rows, total, 1, bot.today()))
            return
        pages = math.ceil(total / TASK_PAGE_SIZE)
        page = min(page, pages)
        if page != 1 and not rows:
            rows, total = bot.store.board(
                guild, page=page, size=TASK_PAGE_SIZE, **filters)
        result = task_list_page(rows, total, page, bot.today())
        view = TaskListView(bot.store, guild, filters, page, total, bot.today())
        view.owner_id = interaction.user.id
        await interaction.edit_original_response(content=result, view=view)

    @group.command(name="mine", description="Показать задачи, которые ты взял или выполняешь")
    @app_commands.describe(page="Начальная страница списка")
    async def my_tasks(interaction: discord.Interaction,
                       page: app_commands.Range[int, 1, 10000] = 1):
        await interaction.response.defer(ephemeral=True, thinking=True)
        filters = {
            "status": None,
            "area": None,
            "assignee": interaction.user.id,
            "team_role": None,
            "priority": None,
        }
        rows, total = bot.store.board(
            guild, page=page, size=TASK_PAGE_SIZE, **filters)
        if not total:
            await interaction.edit_original_response(
                content="У тебя пока нет взятых задач.")
            return
        pages = math.ceil(total / TASK_PAGE_SIZE)
        page = min(page, pages)
        if not rows:
            rows, total = bot.store.board(
                guild, page=page, size=TASK_PAGE_SIZE, **filters)
        view = TaskListView(bot.store, guild, filters, page, total, bot.today())
        view.owner_id = interaction.user.id
        await interaction.edit_original_response(
            content=task_list_page(rows, total, page, bot.today()), view=view)

    @group.command(name="show", description="Открыть карточку задачи")
    @app_commands.describe(task_id="Номер задачи, без символа #")
    async def show(interaction: discord.Interaction, task_id: app_commands.Range[int, 1]):
        row = bot.store.get(guild, task_id)
        await interaction.response.send_message(task_detail(row, bot.today()), ephemeral=True, suppress_embeds=True)

    @group.command(name="status", description="Изменить статус задачи")
    @app_commands.describe(task_id="Номер задачи", status="Новый статус",
                           reason="Для «Заблокировано»: что мешает и какая помощь нужна")
    @app_commands.choices(status=STATUS_CHOICES)
    async def status_cmd(interaction: discord.Interaction, task_id: app_commands.Range[int, 1],
                         status: str, reason: str | None = None):
        row = bot.store.set_status(guild, task_id, interaction.user.id,
                                   bot.is_manager(interaction), status, reason,
                                   actor_roles=actor_roles(interaction))
        await interaction.response.send_message("Статус обновлён:\n" + task_detail(row, bot.today()),
                                                ephemeral=True, suppress_embeds=True)

    @group.command(name="edit", description="Обновить название, описание, исполнителя или срок")
    @app_commands.describe(task_id="Номер задачи", title="Новое название", area="Новое направление",
                           details="Новое описание", assignee="Новый ответственный",
                           due="Новый срок: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ",
                           clear_assignee="Убрать ответственного", clear_due="Убрать срок",
                           team_role="Новая роль задачи", clear_team_role="Убрать роль задачи",
                           priority="Новый приоритет")
    @app_commands.choices(area=AREA_CHOICES, priority=PRIORITY_CHOICES)
    async def edit(interaction: discord.Interaction, task_id: app_commands.Range[int, 1],
                   title: str | None = None, area: str | None = None, details: str | None = None,
                   assignee: discord.Member | None = None, due: str | None = None,
                   clear_assignee: bool = False, clear_due: bool = False,
                   team_role: discord.Role | None = None, clear_team_role: bool = False,
                   priority: str | None = None):
        check_task_assignment(interaction, team_role, assignee)
        existing = bot.store.get(guild, task_id)
        if assignee is not None and team_role is None and not clear_team_role and existing["team_role"]:
            if assignee.get_role(int(existing["team_role"])) is None:
                raise InputError("Ответственный должен состоять в роли задачи.")
        if (team_role is not None and str(team_role.id) != existing["team_role"]
                and assignee is None and not clear_assignee and existing["assignee"]):
            raise InputError("При смене роли заново выбери исполнителя из неё или убери исполнителя.")
        row = bot.store.edit(guild, task_id, interaction.user.id, bot.is_manager(interaction),
                             title=title, area=area, description=details,
                             assignee=assignee.id if assignee else None, due=due,
                             clear_assignee=clear_assignee, clear_due=clear_due,
                             team_role=team_role.id if team_role else None, clear_team_role=clear_team_role,
                             priority=priority, actor_roles=actor_roles(interaction))
        await interaction.response.send_message("Задача обновлена:\n" + task_detail(row, bot.today()),
                                                ephemeral=True, suppress_embeds=True)

    @group.command(name="claim", description="Взять задачу своей роли, у которой нет исполнителя")
    @app_commands.describe(task_id="Номер задачи, без символа #")
    async def claim(interaction: discord.Interaction, task_id: app_commands.Range[int, 1]):
        row = bot.store.claim(guild, task_id, interaction.user.id, actor_roles(interaction))
        await interaction.response.send_message("Задача теперь у тебя:\n" + task_detail(row, bot.today()),
                                                ephemeral=True, suppress_embeds=True)

    @group.command(name="history", description="Показать последние изменения задачи")
    @app_commands.describe(task_id="Номер задачи, без символа #")
    async def history(interaction: discord.Interaction, task_id: app_commands.Range[int, 1]):
        rows = bot.store.history(guild, task_id, limit=6)
        lines = [f"**Журнал задачи #{task_id}**"]
        for row in rows:
            actor = f"<@{row['actor']}>" if row["actor"] else "система"
            lines.append(f"• {row['created_at']} UTC · {actor}: {safe(row['detail'], 115)}")
        if not rows:
            lines.append("Новых записей нет. Изменения до обновления могут отсутствовать.")
        await interaction.response.send_message("\n".join(lines)[:1950], ephemeral=True,
                                                suppress_embeds=True)

    @group.command(name="delete", description="Удалить задачу (автору или руководителю)")
    @app_commands.describe(task_id="Номер задачи", confirm="Подтвердить удаление")
    async def delete(interaction: discord.Interaction,
                     task_id: app_commands.Range[int, 1], confirm: bool = False):
        if not confirm:
            await interaction.response.send_message(
                f"Чтобы удалить задачу #{task_id}, повтори команду с confirm: Да.",
                ephemeral=True)
            return
        row = bot.store.delete(guild, task_id, interaction.user.id, bot.is_manager(interaction))
        await interaction.response.send_message(
            f"Задача #{task_id} «{safe(row['title'], 120)}» удалена. История сохранена.",
            ephemeral=True, suppress_embeds=True)
    return group


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "PASTE_TOKEN_HERE":
        raise SystemExit("Укажи DISCORD_TOKEN в .env или в секретах хостинга. Не отправляй его в чат.")
    guild_id = read_id("DISCORD_GUILD_ID", required=True)
    try:
        tz = ZoneInfo(os.getenv("PROJECT_TIMEZONE", "Europe/Tallinn"))
        report_time = datetime.strptime(os.getenv("SUMMARY_TIME", "18:00"), "%H:%M").time()
    except (ValueError, ZoneInfoNotFoundError):
        raise SystemExit("Проверь PROJECT_TIMEZONE (например Europe/Tallinn) и SUMMARY_TIME (18:00).")
    project_channel = read_id("PROJECT_CHANNEL_ID")
    summary_flag = os.getenv("SUMMARY_ENABLED", "0").strip()
    if summary_flag not in ("0", "1"):
        raise SystemExit("SUMMARY_ENABLED должен быть 0 (выключено) или 1 (включено).")
    summary_enabled = summary_flag == "1"
    if summary_enabled and not project_channel:
        raise SystemExit("Для ежедневной сводки укажи PROJECT_CHANNEL_ID.")
    bot = Tracker(guild_id, Store(os.getenv("DATABASE_PATH", "./data/tasks.sqlite3")), tz,
                  read_id("TEAM_ROLE_ID"), read_id("MANAGER_ROLE_ID"),
                  project_channel, summary_enabled, report_time, read_role_ids("ROLE_MANAGE_IDS"))
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
