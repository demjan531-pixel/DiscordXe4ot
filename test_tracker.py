from datetime import date, time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from tracker_store import InputError, Store, deadline


class DatabaseFixture(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "test.sqlite3"
        self.store = Store(self.path)


class DatabaseTests(DatabaseFixture):
    def test_task_survives_restart_and_is_isolated(self):
        row = self.store.create(1, 12, "Удар  мечом", "gameplay", "Прототип", 24, "01.04.2027")
        reopened = Store(self.path)
        result = reopened.get(1, row["id"])
        self.assertEqual((result["title"], result["assignee"], result["due"], result["status"]),
                         ("Удар мечом", "24", "2027-04-01", "open"))
        self.assertEqual(reopened.board(2), ([], 0))
        with self.assertRaises(InputError):
            reopened.get(2, row["id"])
        with self.assertRaises(InputError):
            reopened.set_status(2, row["id"], 12, True, "done")

    def test_calendar(self):
        self.assertEqual(deadline(" 29.02.2028 "), "2028-02-29")
        self.assertEqual(deadline("2028-02-29"), "2028-02-29")
        for value in ("", "29.02.2027", "2027-1-01", "31.04.2027", "tomorrow"):
            with self.subTest(value=value), self.assertRaises(InputError):
                deadline(value)

    def test_mutation_permissions_and_blocked_reason(self):
        row = self.store.create(1, 12, "Прототип боя", "gameplay", assignee=24)
        key = row["id"]
        with self.assertRaises(InputError):
            self.store.set_status(1, key, 25, False, "done")
        with self.assertRaises(InputError):
            self.store.set_status(1, key, 24, False, "blocked", " ")
        with self.assertRaises(InputError):
            self.store.set_status(1, key, 24, False, "done", "неожиданная причина")
        updated = self.store.set_status(1, key, 24, False, "blocked", "Ждём карту")
        self.assertEqual((updated["status"], updated["reason"]), ("blocked", "Ждём карту"))
        completed = self.store.set_status(1, key, 37, True, "done")
        self.assertEqual((completed["status"], completed["reason"]), ("done", ""))

    def test_edit_reassignment_and_clearing(self):
        key = self.store.create(1, 12, "Меню", "ui", assignee=24, due="2027-04-01")["id"]
        with self.assertRaises(InputError):
            self.store.edit(1, key, 25, False, title="Меню 2")
        with self.assertRaises(InputError):
            self.store.edit(1, key, 12, False, assignee=20, clear_assignee=True)
        with self.assertRaises(InputError):
            self.store.edit(1, key, 12, False)
        updated = self.store.edit(1, key, 12, False, title="Меню 2", area="qa",
                                  assignee=25, clear_due=True)
        self.assertEqual((updated["title"], updated["area"], updated["assignee"], updated["due"]),
                         ("Меню 2", "qa", "25", None))
        with self.assertRaises(InputError):
            self.store.edit(1, key, 24, False, title="Не ответственный")
        self.assertIsNone(self.store.edit(1, key, 25, False, clear_assignee=True)["assignee"])

    def test_filters_pagination_and_progress(self):
        today = date(2027, 4, 2)
        first = self.store.create(1, 12, "A", "qa", assignee=24, due="2027-04-01")
        second = self.store.create(1, 12, "B", "qa", assignee=24, due="2027-04-02")
        finished = self.store.create(1, 12, "C", "maps", due="2027-03-21")
        self.store.create(2, 12, "D", "maps", due="2027-03-21")
        self.store.set_status(1, first["id"], 12, False, "blocked", "Нет ассета")
        self.store.set_status(1, finished["id"], 12, False, "done")
        rows, total = self.store.board(1, area="qa", assignee=24, page=2, size=1)
        self.assertEqual(([r["id"] for r in rows], total), ([second["id"]], 2))
        rows, total = self.store.board(1, status="blocked")
        self.assertEqual(([r["id"] for r in rows], total), ([first["id"]], 1))
        counts, overdue, blocked = self.store.progress(1, today)
        self.assertEqual(sum(row["n"] for row in counts), 3)
        self.assertEqual((overdue, [r["id"] for r in blocked]), (1, [first["id"]]))
        self.assertEqual([r["id"] for r in self.store.due_tasks(1, today)],
                         [first["id"], second["id"]])

    def test_daily_checkpoint_and_validation(self):
        today = date(2027, 4, 2)
        self.assertFalse(self.store.summary_sent(1, today))
        self.store.mark_summary_sent(1, today)
        self.store.mark_summary_sent(1, today)
        self.assertTrue(Store(self.path).summary_sent(1, today))
        self.assertFalse(self.store.summary_sent(2, today))
        with self.assertRaises(InputError):
            self.store.create(1, 12, " ", "qa")
        with self.assertRaises(InputError):
            self.store.create(1, 12, "Bad area", "nope")
        with self.assertRaises(InputError):
            self.store.create(1, 12, "x" * 121, "qa")


class RoleTaskTests(DatabaseFixture):
    def test_migrates_v1_database_without_losing_tasks_or_summary_checkpoint(self):
        saved = Path(self.temp.name) / "version1.sqlite3"
        with sqlite3.connect(saved) as db:
            db.executescript("""
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL,
                    title TEXT NOT NULL, area TEXT NOT NULL, description TEXT NOT NULL,
                    creator TEXT NOT NULL, assignee TEXT, due TEXT,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','doing','review','blocked','done')),
                    reason TEXT NOT NULL DEFAULT '',
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX task_board ON tasks(guild, status, due, id);
                CREATE TABLE summary_log (
                    guild TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY (guild, day)
                );
                INSERT INTO tasks
                    (id, guild, title, area, description, creator, assignee, due, status, reason)
                VALUES (40, '1', 'Модель', 'art', 'Резервная копия', '12', '24',
                        '2027-04-03', 'blocked', 'Нужен референс');
                INSERT INTO summary_log VALUES ('1', '2027-04-02');
            """)
        migrated = Store(saved)
        migrated = Store(saved)  # Migration is idempotent.
        row = migrated.get(1, 40)
        self.assertEqual(
            (row["title"], row["assignee"], row["due"], row["status"], row["reason"],
             row["team_role"], row["priority"]),
            ("Модель", "24", "2027-04-03", "blocked", "Нужен референс", None, "normal"),
        )
        self.assertTrue(migrated.summary_sent(1, date(2027, 4, 2)))
        self.assertGreater(migrated.create(1, 12, "Продолжение", "art")["id"], 40)

    def test_role_claim_requires_membership_and_only_one_concurrent_claim_succeeds(self):
        target = self.store.create(1, 12, "Тест раунда", "qa", team_role=777)["id"]
        with self.assertRaises(InputError):
            self.store.claim(1, target, 24, (888,))
        with self.assertRaises(InputError):
            self.store.claim(2, target, 24, (777,))
        self.assertIsNone(self.store.get(1, target)["assignee"])

        start = Barrier(2, timeout=5)
        def race(actor):
            store = Store(self.path)
            start.wait()
            try:
                return store.claim(1, target, actor, (777,))["assignee"]
            except InputError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            contenders = [pool.submit(race, actor) for actor in (21, 22)]
            winners = [result for future in contenders
                       if (result := future.result(timeout=7)) is not None]
        self.assertEqual(len(winners), 1)
        row = self.store.get(1, target)
        self.assertEqual((row["assignee"], row["status"], row["team_role"]),
                         (winners[0], "doing", "777"))

    def test_only_role_task_in_eligible_status_can_be_claimed(self):
        for kwargs in ({}, {"team_role": 777, "assignee": 18}):
            task_id = self.store.create(1, 12, "Занято", "qa", **kwargs)["id"]
            with self.assertRaises(InputError):
                self.store.claim(1, task_id, 24, (777,))
        task_id = self.store.create(1, 12, "Готово", "qa", team_role=777)["id"]
        self.store.set_status(1, task_id, 12, False, "done")
        with self.assertRaises(InputError):
            self.store.claim(1, task_id, 24, (777,))

    def test_team_assignment_respects_existing_acl(self):
        target = self.store.create(1, 12, "Модель", "art", team_role=777, assignee=24)["id"]
        with self.assertRaises(InputError):
            self.store.set_status(1, target, 30, False, "done", actor_roles=(777,))
        with self.assertRaises(InputError):
            self.store.set_status(1, target, 24, False, "doing")
        with self.assertRaises(InputError):
            self.store.edit(1, target, 24, False, title="Без роли")
        updated = self.store.set_status(1, target, 24, False, "review", actor_roles=(777,))
        self.assertEqual(updated["status"], "review")
        updated = self.store.edit(1, target, 12, False, clear_team_role=True)
        self.assertEqual((updated["team_role"], updated["assignee"]), (None, "24"))
        self.assertEqual(self.store.set_status(1, target, 24, False, "done")["status"], "done")

    def test_filtered_progress_priorities_and_journal(self):
        today = date(2027, 4, 2)
        first = self.store.create(1, 12, "Оружие", "gameplay", due="2027-04-01",
                                  team_role=777, priority="urgent")["id"]
        self.store.create(1, 12, "Карта", "maps", due="2027-04-01", team_role=777)
        self.store.create(1, 12, "Камера", "gameplay", due="2027-04-01", team_role=888)
        self.store.create(2, 12, "Чужой проект", "gameplay", team_role=777)
        with self.assertRaises(InputError):
            self.store.create(1, 12, "Неверный приоритет", "qa", priority="never")
        self.assertEqual([r["id"] for r in self.store.board(
            1, team_role=777, priority="urgent")[0]], [first])
        self.store.claim(1, first, 24, (777,))
        self.store.edit(1, first, 12, False, priority="high")
        self.store.set_status(1, first, 24, False, "blocked", "Нет модели", actor_roles=(777,))
        before = list(self.store.history(1, first, limit=20))
        with self.assertRaises(InputError):
            self.store.edit(1, first, 999, False, title="Не должно сохраниться")
        self.assertEqual(list(self.store.history(1, first, limit=20)), before)
        self.assertEqual(len(before), 4)
        self.assertEqual({str(r["actor"]) for r in before}, {"12", "24"})
        self.assertTrue(all(r["action"] and r["detail"] and r["created_at"] for r in before))
        self.assertEqual(len(self.store.history(1, first, limit=2)), 2)
        with self.assertRaises(InputError):
            self.store.history(2, first)
        counts, overdue, blocked = self.store.progress(1, today, area="gameplay", team_role=777)
        self.assertEqual(sum(r["n"] for r in counts), 1)
        self.assertEqual((overdue, [r["id"] for r in blocked]), (1, [first]))
        self.assertEqual([r["id"] for r in self.store.due_tasks(
            1, today, area="gameplay", team_role=777)], [first])


@skipUnless(find_spec("discord") is not None, "Установи discord.py из requirements.txt")
class DiscordTests(TestCase):
    def setUp(self):
        from bot import Tracker
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")
        self.bot = Tracker(100, self.store, ZoneInfo("Europe/Tallinn"), None, None, 300,
                           False, time(18))

    def test_command_schema_and_intents(self):
        commands = self.bot.tree.get_commands(guild=self.bot.guild)
        self.assertEqual({command.name for command in commands}, {"task", "progress", "role"})
        group = self.bot.tree.get_command("task", guild=self.bot.guild)
        self.assertEqual({cmd.name for cmd in group.commands},
                         {"create", "list", "mine", "show", "status", "edit", "claim", "history"})
        role_group = self.bot.tree.get_command("role", guild=self.bot.guild)
        self.assertEqual({cmd.name for cmd in role_group.commands}, {"grant", "remove", "list"})
        create = group.get_command("create")
        self.assertTrue({"team_role", "priority"} <= {param.name for param in create.parameters})
        progress = self.bot.tree.get_command("progress", guild=self.bot.guild)
        self.assertEqual({param.name for param in progress.parameters}, {"area", "team_role"})
        self.assertFalse(self.bot.intents.message_content)
        self.assertFalse(self.bot.intents.members)
        self.assertFalse(self.bot.allowed_mentions.everyone)

    def test_command_guild_and_channel_checks(self):
        async def run(guild_id, channel_id):
            response = SimpleNamespace(send_message=AsyncMock())
            interaction = SimpleNamespace(guild_id=guild_id, channel_id=channel_id, response=response)
            result = await self.bot.tree.interaction_check(interaction)
            return result, response.send_message.await_count
        self.assertEqual(asyncio.run(run(200, 300)), (False, 1))
        self.assertEqual(asyncio.run(run(100, 400)), (False, 1))
        self.assertEqual(asyncio.run(run(100, 300)), (True, 0))

    def test_safe_progress_render(self):
        from bot import summary
        today = date(2027, 4, 2)
        row = self.store.create(100, 12, "@everyone **Дверь**", "maps",
                                assignee=24, due="2027-04-01")
        self.store.set_status(100, row["id"], 12, False, "blocked", "Ждём модель")
        body = summary(self.store, 100, today)
        self.assertNotIn("@everyone", body)
        self.assertIn("Просрочено: **1**", body)
        self.assertIn("Ждём модель", body)
        self.assertLessEqual(len(body), 2000)

    def _role_change_fixture(self):
        import discord

        class Role(SimpleNamespace):
            def __ge__(self, other):
                return self.rank >= other.rank

            def is_default(self):
                return self.id == self.guild.id

        self.bot.manager_role = 55
        self.bot.role_manage_ids = frozenset({777})
        channel = SimpleNamespace(
            flags=SimpleNamespace(value=0), overwrites_for=Mock(),
            permissions_for=Mock(return_value=discord.Permissions(view_channel=True)),
        )
        channel.overwrites_for.return_value = SimpleNamespace(
            pair=lambda: (discord.Permissions.none(), discord.Permissions.none()))
        me = SimpleNamespace(top_role=Role(rank=20),
                             guild_permissions=discord.Permissions(manage_roles=True))
        guild = SimpleNamespace(id=100, owner_id=12, unavailable=False, me=me,
                                channels=[channel])
        actor = Mock(spec=discord.Member)
        actor.id = 21
        actor.get_role.return_value = object()
        actor.top_role = Role(rank=15)
        actor.guild_permissions = discord.Permissions(manage_roles=True)
        interaction = SimpleNamespace(
            guild_id=100, channel_id=300, guild=guild, user=actor,
            permissions=discord.Permissions(manage_roles=True),
        )
        member = SimpleNamespace(id=24, guild=guild, bot=False, top_role=Role(rank=2))
        role = Role(id=777, guild=guild, managed=False, rank=5,
                    permissions=discord.Permissions.none())
        return interaction, member, role, channel

    def test_role_change_requires_authorized_operator_and_safe_scope(self):
        interaction, member, role, _ = self._role_change_fixture()
        self.bot.check_role_change(interaction, member, role)
        interaction.channel_id = 500
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)
        interaction.channel_id = 300
        interaction.user.get_role.return_value = None
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)
        interaction.user.id = interaction.guild.owner_id
        self.bot.check_role_change(interaction, member, role)
        self.bot.role_manage_ids = frozenset()
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)

    def test_role_change_rejects_elevated_bits_overwrites_and_peer_targets(self):
        import discord
        interaction, member, role, channel = self._role_change_fixture()
        role.permissions = discord.Permissions(manage_roles=True)
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)

        role.permissions = discord.Permissions.none()
        for allow, deny in (
            (discord.Permissions(view_channel=True), discord.Permissions.none()),
            (discord.Permissions.none(), discord.Permissions(view_channel=True)),
        ):
            with self.subTest(allow=allow.value, deny=deny.value):
                channel.overwrites_for.return_value.pair = lambda: (allow, deny)
                with self.assertRaises(InputError):
                    self.bot.check_role_change(interaction, member, role)
        channel.overwrites_for.return_value.pair = lambda: (
            discord.Permissions.none(), discord.Permissions.none())
        member.top_role.rank = interaction.user.top_role.rank
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)
        member.top_role.rank = 1
        member.id = interaction.user.id
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)

    def test_role_change_refuses_to_infer_permissions_of_hidden_channels(self):
        import discord
        interaction, member, role, channel = self._role_change_fixture()
        channel.flags.value = 1 << 17
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)
        channel.flags.value = 0
        channel.permissions_for.return_value = discord.Permissions.none()
        with self.assertRaises(InputError):
            self.bot.check_role_change(interaction, member, role)
