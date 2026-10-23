"""Loose ends dashboard routes, mounted at /api/plugins/loose-ends/ behind the dashboard's auth.

Logic and state live in ../loose_ends.py (stdlib only), shared with the sweep cron job.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import loose_ends  # noqa: E402

router = APIRouter()
PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


@router.get("/items")
def items() -> dict:
    return loose_ends.view()


@router.post("/decide")
def decide(body: dict) -> dict:
    decisions = body.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(status_code=400, detail="decisions must be a non-empty list")
    clean = []
    for d in decisions:
        if not isinstance(d, dict) or d.get("action") not in ("done", "tomorrow", "next"):
            raise HTTPException(status_code=400, detail=f"bad decision: {d!r}")
        if d["action"] == "next" and not str(d.get("note") or "").strip():
            raise HTTPException(status_code=400, detail="next steps need a note")
        clean.append({"id": str(d.get("id")), "action": d["action"], "note": str(d.get("note") or "")})
    return {"results": loose_ends.decide(clean), "view": loose_ends.view()}


@router.post("/setup")
def setup(body: dict) -> dict:
    """Puts the sweep's pre-run script in `profile`'s scripts folder and answers with the cron job
    to create there (POST /api/cron/jobs?profile=…). The client creates it, so it shows up and
    can be changed like any other scheduled task."""
    profile = str(body.get("profile") or "").strip()
    if not PROFILE.fullmatch(profile):
        raise HTTPException(status_code=400, detail="profile must be a profile name")
    home = loose_ends.HERMES if profile == "default" else loose_ends.HERMES / "profiles" / profile
    if not (home / "config.yaml").exists() and profile != "default":
        raise HTTPException(status_code=404, detail=f"no profile named {profile}")
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "loose-ends-prepare.sh"
    script.write_text(loose_ends.sweep_script())
    script.chmod(0o755)
    return {"job": {"name": loose_ends.SWEEP_JOB, "schedule": "0 9,16 * * *", "script": script.name,
                    "prompt": loose_ends.sweep_prompt(), "deliver": f"bot-chat:{profile}"}}
