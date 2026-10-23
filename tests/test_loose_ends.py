import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loose_ends as le  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(le, n, v) for n, v in {
            "STATE": d, "ITEMS": d / "items.json", "LOCK": d / ".lock",
            "EVENTS": d / "events.log", "DIGESTS": d / "digests"}.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def add(self, **kw):
        n = {"bot": "main", "title": "Notion deletion", "detail": "d", "ask": "send?"}
        n.update(kw)
        return le.apply({"new": [n]})["added"]


class ApplyTests(Base):
    def test_new_items_dedupe_by_bot_and_title(self):
        self.assertEqual(len(self.add()), 1)
        self.assertEqual(self.add(title="notion DELETION"), [])
        self.assertEqual(len(self.add(bot="household")), 1)
        self.assertEqual(len(le.view()["waiting"]), 2)

    def test_resolve_and_update_only_touch_open_items(self):
        [i] = self.add()
        le.apply({"update": [{"id": i, "ask": "new ask", "status": "closed"}]})
        item = le.load()["items"][0]
        self.assertEqual((item["ask"], item["status"]), ("new ask", "open"))  # status not writable
        le.apply({"resolve": [{"id": i, "reason": "You replied in chat"}]})
        self.assertEqual(le.load()["items"][0]["status"], "closed")
        self.assertEqual(le.view()["closed"][0]["id"], i)


class DecideTests(Base):
    def test_done_tomorrow_next(self):
        a, b, c = self.add(title="a")[0], self.add(title="b")[0], self.add(title="c")[0]
        spawned = []
        res = le.decide([{"id": a, "action": "done", "note": ""},
                         {"id": b, "action": "tomorrow", "note": "after the call"},
                         {"id": c, "action": "next", "note": "send the draft"}], spawn=spawned.append)
        self.assertEqual(res, {a: "done", b: "tomorrow", c: "next"})
        self.assertEqual(spawned, [c])
        items = {i["id"]: i for i in le.load()["items"]}
        self.assertEqual(items[a]["status"], "closed")
        self.assertEqual(items[b]["status"], "snoozed")
        self.assertIn("after the call", items[b]["detail"])
        self.assertEqual(items[c]["status"], "handing_off")
        v = le.view()
        self.assertEqual([i["id"] for i in v["working"]], [c])
        self.assertEqual([i["id"] for i in v["snoozed"]], [b])
        self.assertEqual(v["waiting"], [])

    def test_snoozed_item_returns_next_morning(self):
        [b] = self.add()
        le.decide([{"id": b, "action": "tomorrow"}], spawn=lambda _: None)
        item = le.load()["items"][0]
        until = dt.datetime.fromisoformat(item["snooze_until"])
        self.assertEqual(until.hour, 8)
        self.assertFalse(le.is_visible(item, until - dt.timedelta(minutes=1)))
        self.assertTrue(le.is_visible(item, until))

    def test_next_without_note_and_decided_items_are_skipped(self):
        [a] = self.add()
        self.assertEqual(le.decide([{"id": a, "action": "next", "note": " "}], spawn=lambda _: None), {a: "skipped"})
        le.decide([{"id": a, "action": "done"}], spawn=lambda _: None)
        self.assertEqual(le.decide([{"id": a, "action": "tomorrow"}], spawn=lambda _: None), {a: "skipped"})


class HandoffTests(Base):
    def _handing(self):
        [c] = self.add()
        le.decide([{"id": c, "action": "next", "note": "send it"}], spawn=lambda _: None)
        return c

    def test_reply_closes_item(self):
        c = self._handing()
        with mock.patch.object(le, "ask_bot", return_value=(0, "Sent the draft to Notion support.")) as ask:
            le.handoff(c)
        bot, prompt = ask.call_args.args
        self.assertEqual(bot, "main")
        self.assertIn("Their instructions: send it", prompt)
        item = le.load()["items"][0]
        self.assertEqual(item["status"], "closed")
        self.assertIn("Sent the draft", item["resolution"])

    def test_failure_reopens_item(self):
        c = self._handing()
        with mock.patch.object(le, "ask_bot", return_value=(1, "")):
            le.handoff(c)
        item = le.load()["items"][0]
        self.assertEqual(item["status"], "open")
        self.assertIn("Hand-off to @main failed", item["detail"])


class SummaryTests(Base):
    def test_silent_when_nothing_waits(self):
        self.assertEqual(le.summary(), "[SILENT]")

    def test_lists_items(self):
        ids = self.add(title="x") + self.add(title="y", bot="household")
        out = le.summary(ids)
        self.assertIn("2 loose ends waiting on you, 2 new", out)
        self.assertIn("- @household: y", out)


class CollectTests(Base):
    def test_digest_from_fake_profile(self):
        home = Path(self.tmp.name) / "home"
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"name": "Mail", "deliver": "telegram:1"}, {"name": "Quiet", "deliver": "local"},
            {"name": "Sweep", "deliver": "bot-chat:x"}]}))
        import sqlite3
        con = sqlite3.connect(home / "state.db")
        con.executescript("""
            CREATE TABLE sessions (id TEXT, source TEXT, title TEXT, started_at REAL, last_activity_at REAL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                                   tool_calls TEXT, timestamp REAL);""")
        t = le.now().timestamp()
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
            ("s1", "desktop", "Bot Chat", t - 100, t), ("c1", "cron", "Mail · Oct 7", t - 90, t),
            ("c2", "cron", "Quiet · Oct 7", t - 80, t), ("c3", "cron", "Sweep · Oct 7", t - 70, t)])
        con.executemany("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)", [
            ("s1", "user", "Can you book Peachy?", None, t - 60),
            ("s1", "assistant", "", '[{"x":1}]', t - 50),
            ("s1", "assistant", "Which day works, Thu or Fri?", None, t - 40),
            ("s1", "assistant", "NO_REPLY", None, t - 30),
            ("c1", "assistant", "Northwest mail needs a login.", None, t - 20),
            ("c2", "assistant", "local only", None, t - 20),
            ("c3", "assistant", "bot chat delivery", None, t - 20)])
        con.commit()
        con.close()
        with mock.patch.object(le, "profiles", return_value=[("main", home)]):
            out = le.collect(le.now() - dt.timedelta(hours=1))
        self.assertIn("**You:** Can you book Peachy?", out)
        self.assertIn("**@main:** Which day works", out)
        self.assertIn("Northwest mail needs a login.", out)
        for absent in ("NO_REPLY", "local only", "bot chat delivery"):
            self.assertNotIn(absent, out)


class SetupTests(Base):
    def test_ask_bot_uses_the_hermes_cli_with_the_profile(self):
        done = mock.Mock(returncode=0, stdout="Done.\n", stderr="")
        with mock.patch.object(le, "hermes_command", return_value=["hermes"]), mock.patch.object(le.subprocess, "run", return_value=done) as run:
            self.assertEqual(le.ask_bot("main", "hi"), (0, "Done."))
            self.assertEqual(run.call_args.args[0], ["hermes", "-p", "main", "chat", "-Q", "--oneshot", "-q", "hi"])
            le.ask_bot("default", "hi")
            self.assertEqual(run.call_args.args[0][:2], ["hermes", "chat"])

    def test_prepare_writes_digest_and_open_items(self):
        self.add(title="Sign the form")
        with mock.patch.object(le, "profiles", return_value=[]):
            context = le.prepare()["context"]
        self.assertTrue(Path(context["digest_file"]).exists())
        self.assertIn("Sign the form", Path(context["open_items_file"]).read_text())

    def test_prompt_and_script_point_at_this_install(self):
        prompt, script = le.sweep_prompt(), le.sweep_script()
        self.assertIn(str(Path(le.__file__).resolve()), prompt)
        self.assertNotIn("{python}", prompt)
        self.assertIn("prepare", script)
        self.assertNotIn("Mick", prompt + script)

    def test_state_lives_at_the_root_even_from_a_profile_home(self):
        import importlib, os
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/tmp/h/profiles/main"}, clear=False):
            fresh = importlib.reload(le)
            self.assertEqual(fresh.STATE, Path("/tmp/h/loose-ends"))
        importlib.reload(le)


if __name__ == "__main__":
    unittest.main()
