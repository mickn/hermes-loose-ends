#!/usr/bin/env python3
"""Loose ends: a twice-daily sweep of every Hermes bot for things still waiting on you,
reviewed on a Hermes dashboard tab (and in Hermes Native) where you mark each item
"dealt with", "remind me tomorrow", or give next steps that are handed to the owning bot.

Pieces (stdlib only; the dashboard plugin imports this file):
  collect   digest of every bot's recent conversations + delivered cron output (for the sweep agent)
  apply     merge the sweep agent's proposal (new / resolve / update) into items.json
  decide    record your dashboard decisions; next steps start a background hand-off
  handoff   deliver one item + your instructions to its bot, store the reply
  summary   one-line status for the sweep's message to you

State: $HERMES_HOME/loose-ends/ (items.json, events.log, digests/). Times use the host's time zone.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
# A profile's own HERMES_HOME is <root>/profiles/<name>; state always lives at the root.
if HERMES.parent.name == "profiles":
    HERMES = HERMES.parent.parent
STATE = Path(os.environ.get("LOOSE_ENDS_HOME") or HERMES / "loose-ends")
ITEMS = STATE / "items.json"
LOCK = STATE / ".items.lock"
EVENTS = STATE / "events.log"
DIGESTS = STATE / "digests"
TZ = dt.datetime.now().astimezone().tzinfo
SWEEP_JOB = "loose-ends-sweep"
SILENT = re.compile(r"^\s*(\[SILENT\]|NO_REPLY|HEARTBEAT_OK|SILENT)\s*$", re.I)
OPEN_STATES = ("open", "snoozed")
MAX_MSG = 1500


# --- store ---------------------------------------------------------------------------

def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def iso(t: dt.datetime) -> str:
    return t.astimezone(TZ).isoformat(timespec="seconds")


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load() -> dict:
    try:
        data = json.loads(ITEMS.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", [])
    data.setdefault("last_sweep", None)
    return data


def save(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = ITEMS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, ITEMS)


def event(kind: str, **fields) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(EVENTS, "a") as fh:
        fh.write(json.dumps({"at": iso(now()), "event": kind, **fields}, ensure_ascii=False) + "\n")


def is_visible(item: dict, at: dt.datetime | None = None) -> bool:
    """Waiting on you right now: open, or snoozed until a time that has passed."""
    at = at or now()
    if item["status"] == "open":
        return True
    if item["status"] == "snoozed":
        until = item.get("snooze_until")
        return not until or dt.datetime.fromisoformat(until) <= at
    return False


def view() -> dict:
    """What the dashboard shows."""
    data = load()
    at = now()
    week_ago = at - dt.timedelta(days=7)
    items = data["items"]
    waiting = [i for i in items if is_visible(i, at)]
    working = [i for i in items if i["status"] == "handing_off"]
    later = [i for i in items if i["status"] == "snoozed" and not is_visible(i, at)]
    closed = [i for i in items if i["status"] == "closed"
              and dt.datetime.fromisoformat(i.get("closed_at") or i["first_seen"]) >= week_ago]
    key = lambda i: i.get("source_time") or i["first_seen"]
    return {"waiting": sorted(waiting, key=key, reverse=True),
            "working": sorted(working, key=key, reverse=True),
            "snoozed": sorted(later, key=lambda i: i.get("snooze_until") or ""),
            "closed": sorted(closed, key=lambda i: i.get("closed_at") or "", reverse=True)[:30],
            "last_sweep": data["last_sweep"]}


# --- collect -------------------------------------------------------------------------

def profiles() -> list[tuple[str, Path]]:
    out = [("default", HERMES)]
    for d in sorted((HERMES / "profiles").iterdir()) if (HERMES / "profiles").is_dir() else []:
        if d.is_dir() and (d / "config.yaml").exists():
            out.append((d.name, d))
    return out


def cron_delivery(home: Path) -> dict[str, str]:
    """job name -> deliver target, to drop output of jobs that only log locally."""
    try:
        jobs = json.loads((home / "cron" / "jobs.json").read_text()).get("jobs", [])
    except (OSError, ValueError):
        return {}
    return {j.get("name") or j["id"]: str(j.get("deliver") or "local") for j in jobs}


def clip(text: str, n: int = MAX_MSG) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    return text if len(text) <= n else text[:n] + " …[cut]"


def stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%a %b %d %H:%M")


PER_BOT = 26_000
SKIP_TITLES = ("Email dispatch",)


def collect(since: dt.datetime) -> str:
    """Markdown digest of what each bot said to you (and what you said) since `since`.
    Each bot gets its own size budget, filled from the newest messages backwards."""
    t0 = since.timestamp()
    parts = [f"# Loose-ends digest: {stamp(t0)} → {stamp(now().timestamp())} ({now():%Z})\n"
             "Per bot, newest material is kept; older lines may be dropped when a bot was very busy.\n"]
    for name, home in profiles():
        db = home / "state.db"
        if not db.exists():
            continue
        delivery = cron_delivery(home)
        entries: list[tuple[float, str, str]] = []   # (ts, session header, line)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            sessions = con.execute(
                "SELECT id, source, coalesce(title,'') FROM sessions "
                "WHERE coalesce(last_activity_at, started_at) >= ? AND source NOT IN ('subagent')", (t0,)).fetchall()
            for sid, source, title in sessions:
                if title in SKIP_TITLES:
                    continue
                if source == "cron":
                    job = title.split(" · ")[0]
                    target = delivery.get(job, "local")
                    # Bot-chat deliveries already appear inside that Bot Chat; local ones never reach you.
                    if job.lower().startswith("loose-ends") or target == "local" or target.startswith("bot-chat") \
                            and "telegram" not in target:
                        continue
                    row = con.execute(
                        "SELECT timestamp, content FROM messages WHERE session_id=? AND role='assistant' "
                        "AND length(coalesce(content,''))>0 ORDER BY timestamp DESC, id DESC LIMIT 1",
                        (sid,)).fetchone()
                    if row and row[0] >= t0 and not SILENT.match(row[1] or ""):
                        entries.append((row[0], f"cron job “{job}” → {target}",
                                        f"- [{stamp(row[0])}] **@{name} (cron, session {sid}):** {clip(row[1])}"))
                    continue
                header = f"{source} session “{title or sid}” (session {sid})"
                rows = con.execute(
                    "SELECT timestamp, role, content FROM messages WHERE session_id=? AND timestamp>=? "
                    "AND role IN ('user','assistant') AND length(coalesce(content,''))>0 "
                    "AND (role='user' OR tool_calls IS NULL OR tool_calls='' OR tool_calls='[]') "
                    "ORDER BY timestamp, id", (sid, t0)).fetchall()
                for ts, role, content in rows:
                    if SILENT.match(content or ""):
                        continue
                    c = content.lstrip()
                    if role == "user" and c.startswith("[IMPORTANT: The user has invoked"):
                        continue  # skill boilerplate
                    who = "You" if role == "user" else f"@{name}"
                    if role == "user" and c.startswith("[Cronjob"):
                        who = "cron output delivered to this chat"
                    elif role == "user" and c.startswith("Message from"):
                        who = "another bot"
                    entries.append((ts, header, f"- [{stamp(ts)}] **{who}:** {clip(content)}"))
        finally:
            con.close()
        if not entries:
            continue
        kept, used = [], 0
        for e in sorted(entries, key=lambda e: e[0], reverse=True):
            if used + len(e[2]) > PER_BOT:
                break
            kept.append(e)
            used += len(e[2])
        dropped = len(entries) - len(kept)
        out, last_header = [f"\n## @{name}" + (f"  ({dropped} older lines dropped)" if dropped else "")], None
        for ts, header, line in sorted(kept, key=lambda e: e[0]):
            if header != last_header:
                out.append(f"\n### {header}")
                last_header = header
            out.append(line)
        parts.append("\n".join(out))
    return "\n".join(parts)


# --- apply (sweep agent output) ------------------------------------------------------

def apply(proposal: dict) -> dict:
    """proposal = {"new": [...], "resolve": [{"id", "reason"}], "update": [{"id", fields...}]}."""
    allowed = {"title", "detail", "ask", "source_time", "source_session"}
    added, resolved, updated = [], [], []
    with locked():
        data = load()
        by_id = {i["id"]: i for i in data["items"]}
        for n in proposal.get("new") or []:
            bot = str(n.get("bot") or "").lstrip("@").strip()
            title = str(n.get("title") or "").strip()
            if not bot or not title:
                continue
            dup = next((i for i in data["items"] if i["bot"] == bot and i["title"].lower() == title.lower()
                        and i["status"] in OPEN_STATES + ("handing_off",)), None)
            if dup:
                continue
            item = {"id": uuid.uuid4().hex[:10], "bot": bot, "title": title[:140],
                    "detail": str(n.get("detail") or "")[:2000], "ask": str(n.get("ask") or "")[:400],
                    "source_session": n.get("source_session"), "source_time": n.get("source_time"),
                    "first_seen": iso(now()), "status": "open", "history": []}
            data["items"].append(item)
            added.append(item["id"])
        for r in proposal.get("resolve") or []:
            item = by_id.get(r.get("id"))
            if item and item["status"] in OPEN_STATES:
                item.update(status="closed", closed_at=iso(now()),
                            resolution=f"Sweep: {str(r.get('reason') or 'resolved')[:300]}")
                resolved.append(item["id"])
        for u in proposal.get("update") or []:
            item = by_id.get(u.get("id"))
            if item and item["status"] in OPEN_STATES:
                item.update({k: v for k, v in u.items() if k in allowed and v})
                updated.append(item["id"])
        data["last_sweep"] = iso(now())
        save(data)
    event("sweep", added=added, resolved=resolved, updated=updated)
    return {"added": added, "resolved": resolved, "updated": updated}


# --- decide (dashboard) --------------------------------------------------------------

def tomorrow_morning() -> dt.datetime:
    t = now() + dt.timedelta(days=1)
    return t.replace(hour=8, minute=0, second=0, microsecond=0)


def decide(decisions: list[dict], spawn=None) -> dict:
    """decisions: [{"id", "action": "done"|"tomorrow"|"next", "note": str}]."""
    spawn = spawn or start_handoff
    results, to_hand_off = {}, []
    with locked():
        data = load()
        by_id = {i["id"]: i for i in data["items"]}
        for d in decisions:
            item = by_id.get(d.get("id"))
            action = d.get("action")
            note = str(d.get("note") or "").strip()[:4000]
            if not item or item["status"] not in OPEN_STATES:
                results[d.get("id")] = "skipped"
                continue
            entry = {"at": iso(now()), "action": action, "note": note}
            if action == "done":
                item.update(status="closed", closed_at=iso(now()),
                            resolution="You: dealt with" + (f" ({note})" if note else ""))
            elif action == "tomorrow":
                item.update(status="snoozed", snooze_until=iso(tomorrow_morning()))
                if note:
                    item["detail"] = (item.get("detail") or "") + f"\n\nYour note: {note}"
            elif action == "next" and note:
                item.update(status="handing_off", handoff={"note": note, "at": iso(now()), "reply": None})
                to_hand_off.append(item["id"])
            else:
                results[item["id"]] = "skipped"
                continue
            item.setdefault("history", []).append(entry)
            results[item["id"]] = action
        save(data)
    event("decide", decisions=decisions)
    for item_id in to_hand_off:
        spawn(item_id)
    return results


def start_handoff(item_id: str) -> None:
    """Run the hand-off in the background (a bot turn can take minutes)."""
    STATE.mkdir(parents=True, exist_ok=True)
    log = open(STATE / "handoff.log", "a")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "handoff", item_id],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def hermes_command() -> list[str]:
    """How to run the Hermes CLI from here: the `hermes` on PATH, else this Python's own module."""
    found = shutil.which("hermes")
    return [found] if found else [sys.executable, "-m", "hermes_cli.main"]


def ask_bot(bot: str, prompt: str, timeout: int = 1800) -> tuple[int, str]:
    """One turn with `bot` through the Hermes CLI; returns (exit code, final reply)."""
    profile = [] if bot in ("default", "hermes") else ["-p", bot]
    done = subprocess.run(hermes_command() + profile + ["chat", "-Q", "--oneshot", "-q", prompt],
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    reply = (done.stdout or "").strip() or (done.stderr or "").strip()[-600:]
    return done.returncode, reply


def handoff_prompt(item: dict) -> str:
    return (
        "The user reviewed their loose ends and handed this item back to you with next steps. "
        "Do the work now, end to end, using your normal tools and their standing permissions; only stop "
        "for a decision that is genuinely his (new spend, legal/medical/financial commitments, "
        "relationship-affecting messages, destructive deletes).\n\n"
        f"Item: {item['title']}\n"
        f"Context: {item.get('detail') or '(none)'}\n"
        f"What was waiting on them: {item.get('ask') or '(none)'}\n"
        f"Originally from: {item.get('source_time') or 'unknown time'}"
        f"{' (session ' + item['source_session'] + ')' if item.get('source_session') else ''}\n\n"
        f"Their instructions: {item['handoff']['note']}\n\n"
        "Your final message is shown to them in Loose ends: say plainly what "
        "you did and the verified result, or the one thing you still need from them."
    )


def handoff(item_id: str) -> int:
    with locked():
        data = load()
        item = next((i for i in data["items"] if i["id"] == item_id), None)
    if not item or item["status"] != "handing_off":
        return 0
    try:
        code, reply = ask_bot(item["bot"], handoff_prompt(item))
    except Exception as exc:  # record and surface on the dashboard
        code, reply = 1, f"{type(exc).__name__}: {exc}"
    reply = (reply or "").strip()
    with locked():
        data = load()
        item = next(i for i in data["items"] if i["id"] == item_id)
        item["handoff"]["reply"] = reply[:4000]
        item["handoff"]["finished_at"] = iso(now())
        if code == 0 and reply:
            item.update(status="closed", closed_at=iso(now()), resolution=f"@{item['bot']}: {reply[:600]}")
        else:
            item.update(status="open", detail=(item.get("detail") or "")
                        + f"\n\nHand-off to @{item['bot']} failed: {reply[:300] or 'no reply'}")
        save(data)
    event("handoff", id=item_id, code=code, reply=reply[:500])
    return 0


# --- summary -------------------------------------------------------------------------

def summary(added: list[str] | None = None) -> str:
    v = view()
    n = len(v["waiting"])
    if not n:
        return "[SILENT]"
    by_bot: dict[str, int] = {}
    for i in v["waiting"]:
        by_bot[i["bot"]] = by_bot.get(i["bot"], 0) + 1
    bots = ", ".join(f"@{b} {c}" for b, c in sorted(by_bot.items(), key=lambda x: -x[1]))
    new = f", {len(added)} new" if added else ""
    top = "\n".join(f"- @{i['bot']}: {i['title']}" for i in v["waiting"][:5])
    more = f"\n- …and {n - 5} more" if n > 5 else ""
    return f"{n} loose end{'s' if n != 1 else ''} waiting on you{new} ({bots}):\n{top}{more}"


# --- sweep setup ---------------------------------------------------------------------

def collect_digest() -> Path:
    data = load()
    default = now() - dt.timedelta(days=2)
    since = dt.datetime.fromisoformat(data["last_sweep"]) - dt.timedelta(hours=2) if data["last_sweep"] else default
    DIGESTS.mkdir(parents=True, exist_ok=True)
    path = DIGESTS / f"digest-{now():%Y%m%d-%H%M}.md"
    path.write_text(collect(since))
    for old in sorted(DIGESTS.glob("digest-*.md"))[:-14]:
        old.unlink()
    return path


def prepare() -> dict:
    """What the cron script hands the sweep agent: where to read, and where to write."""
    digest = collect_digest()
    v = view()
    open_file = STATE / "open-items.json"
    open_file.write_text(json.dumps({"waiting": v["waiting"], "working": v["working"], "snoozed": v["snoozed"]},
                                    indent=1, ensure_ascii=False))
    return {"wakeAgent": True, "context": {"digest_file": str(digest), "open_items_file": str(open_file),
                                            "proposal_file": str(STATE / "proposal.json")}}


def sweep_prompt() -> str:
    """The sweep job's prompt, with this install's own paths."""
    text = (HERE / "sweep-prompt.md").read_text()
    return text.replace("{python}", sys.executable).replace("{loose_ends}", str(Path(__file__).resolve()))


def sweep_script() -> str:
    """The cron pre-run script that prepares each sweep."""
    return f'#!/usr/bin/env bash\n# Loose ends: prepares the sweep (digest of recent bot conversations + open items).\nset -euo pipefail\nexec "{sys.executable}" "{Path(__file__).resolve()}" prepare\n'


# --- CLI -----------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "collect":  # prints the digest path
        print(collect_digest())
        return 0
    if cmd == "apply":  # apply <proposal.json>
        res = apply(json.loads(Path(argv[2]).read_text()))
        print(json.dumps(res))
        return 0
    if cmd == "open":  # current open items for the sweep agent
        v = view()
        print(json.dumps({"waiting": v["waiting"], "working": v["working"], "snoozed": v["snoozed"]},
                         indent=1, ensure_ascii=False))
        return 0
    if cmd == "handoff":
        return handoff(argv[2])
    if cmd == "summary":
        added = []
        try:
            last = [json.loads(l) for l in EVENTS.read_text().splitlines() if '"sweep"' in l][-1]
            added = last.get("added") or []
        except (OSError, IndexError, ValueError):
            pass
        print(summary(added))
        return 0
    if cmd == "prepare":  # the sweep's pre-run step: digest + open items, as cron script context
        print(json.dumps(prepare()))
        return 0
    print("usage: loose_ends.py collect|prepare|apply <file>|open|handoff <id>|summary", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
