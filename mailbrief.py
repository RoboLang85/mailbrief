#!/usr/bin/env python3
"""
mailbrief — local email triage + job-posting digest.

Design principles:
  - No cloud LLM calls. Everything runs against a local Ollama instance.
  - Deterministic plumbing, LLM only for classification/summarization.
    (Local models are reliable at "answer this one question", unreliable
     at multi-step tool loops. This design plays to that.)
  - Idempotent: processed message UIDs are recorded in SQLite, so a crash
    or double-run never reprocesses or double-counts.
  - Read-only against the mailbox. Nothing is sent, deleted, or marked.

Usage:
    ./mailbrief.py init                  # create state dirs + DB
    ./mailbrief.py triage                # fetch, classify, write brief
    ./mailbrief.py jobs                  # fetch + score public job boards
    ./mailbrief.py run                   # triage + jobs, one brief

Credentials come from macOS Keychain, never from a dotfile:
    security add-generic-password -s mailbrief -a you@example.com -w
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
from imap_tools import AND, MailBox

# --------------------------------------------------------------------------
# Config. Override via environment; no secrets live here.
# --------------------------------------------------------------------------

HOME = Path(os.environ.get("MAILBRIEF_HOME", Path.home() / ".mailbrief"))
DB_PATH = HOME / "state.db"
BRIEF_DIR = HOME / "briefs"
DRAFT_DIR = HOME / "drafts"
BOARDS_PATH = HOME / "boards.json"
RESUME_PATH = HOME / "resume.txt"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("MAILBRIEF_MODEL", "qwen3:8b")

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", "")
KEYCHAIN_SERVICE = os.environ.get("MAILBRIEF_KEYCHAIN", "mailbrief")

# Prefill is the bottleneck on Apple Silicon (memory bandwidth is generous,
# GPU compute for prompt processing is not). Truncating hard is the single
# biggest speed lever available -- far more than swapping models.
BODY_CHARS = int(os.environ.get("MAILBRIEF_BODY_CHARS", "2500"))
MAX_MESSAGES = int(os.environ.get("MAILBRIEF_MAX_MESSAGES", "60"))
LOOKBACK_DAYS = int(os.environ.get("MAILBRIEF_LOOKBACK_DAYS", "2"))
HTTP_TIMEOUT = 300

# Job descriptions are long. 6000 chars covers responsibilities + requirements
# on almost every posting; the remainder is benefits boilerplate and EEO text,
# which is pure prefill cost.
JD_CHARS = int(os.environ.get("MAILBRIEF_JD_CHARS", "6000"))
RESUME_CHARS = int(os.environ.get("MAILBRIEF_RESUME_CHARS", "6000"))


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def keychain_secret(account: str, service: str = KEYCHAIN_SERVICE) -> str:
    """Read a password from the macOS Keychain.

    Deliberately shells out to `security` rather than storing a token in a
    dotfile: the secret stays encrypted at rest under the login keychain and
    is never captured in shell history, backups, or a git working tree.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        sys.exit("`security` not found. This credential path is macOS-only.")
    except subprocess.CalledProcessError:
        sys.exit(
            f"No Keychain entry for service={service} account={account}.\n"
            f"Add one with:\n"
            f"  security add-generic-password -s {service} -a {account} -w"
        )
    return result.stdout.strip()


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    uid         TEXT PRIMARY KEY,
    processed   TEXT NOT NULL,
    category    TEXT,
    urgency     INTEGER,
    needs_reply INTEGER,
    sender      TEXT,
    subject     TEXT,
    summary     TEXT,
    action      TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    url         TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    score       INTEGER,
    rationale   TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    url         TEXT PRIMARY KEY,
    created     TEXT NOT NULL,
    path        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_processed ON seen(processed);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
"""


def db() -> sqlite3.Connection:
    for directory in (HOME, BRIEF_DIR, DRAFT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Additive migration for databases created before descriptions were stored.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "description" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
        conn.commit()
    return conn


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def ask(
    system: str,
    user: str,
    schema: dict | None = None,
    temperature: float = 0.2,
    num_ctx: int = 8192,
) -> str:
    """One-shot call to local Ollama.

    Passing a JSON Schema as `format` constrains decoding, so the output is
    guaranteed-parseable JSON. This is what makes an 8B model usable here:
    the failure mode becomes "wrong answer" rather than "unparseable prose",
    and wrong answers are cheap when the label space is six categories.
    """
    body = {
        "model": MODEL,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if schema:
        body["format"] = schema
    try:
        response = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
    except httpx.ConnectError:
        sys.exit(f"Cannot reach Ollama at {OLLAMA_URL}. Is `ollama serve` running?")
    return response.json()["message"]["content"]


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["job", "finance", "personal", "work", "newsletter", "promo", "other"],
        },
        "urgency": {"type": "integer", "minimum": 1, "maximum": 5},
        "needs_reply": {"type": "boolean"},
        "summary": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["category", "urgency", "needs_reply", "summary", "action"],
}

TRIAGE_SYSTEM = (
    "You triage email. Output JSON only.\n"
    "urgency: 5 = needs action today, 3 = this week, 1 = informational.\n"
    "summary: one sentence, under 25 words, no preamble.\n"
    "action: the concrete next step, or an empty string if none.\n"
    "Marketing and automated notifications are urgency 1 regardless of "
    "how urgent their subject line claims to be."
)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def strip_html(raw: str) -> str:
    """Flatten HTML to plain text. Greenhouse double-encodes, hence two passes."""
    text = html.unescape(raw or "")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<(p|br|/p|li|/div|h[1-6])[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_body(message) -> str:
    """Strip an email down to something worth spending prefill tokens on."""
    text = message.text or ""
    if not text.strip() and message.html:
        text = strip_html(message.html)
    # Quoted reply chains and footers are pure prefill cost with no signal.
    text = re.split(r"\n\s*On .{0,80} wrote:\s*\n|\n-{2,}\s*Original Message", text)[0]
    text = re.sub(r"https?://\S{60,}", "[long-url]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:BODY_CHARS]


def triage(conn: sqlite3.Connection, verbose: bool = True) -> list[dict]:
    if not IMAP_USER:
        sys.exit("Set IMAP_USER (e.g. export IMAP_USER=you@example.com).")

    password = keychain_secret(IMAP_USER)
    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS))
    seen_uids = {row["uid"] for row in conn.execute("SELECT uid FROM seen")}
    results: list[dict] = []

    # mark_seen=False: this tool observes the mailbox, it does not mutate it.
    # An automation that silently marks mail read is an automation you stop
    # trusting the first time it eats something important.
    with MailBox(IMAP_HOST).login(IMAP_USER, password, "INBOX") as mailbox:
        messages = list(
            mailbox.fetch(AND(date_gte=since), limit=MAX_MESSAGES,
                          mark_seen=False, reverse=True, bulk=True)
        )

    for message in messages:
        if message.uid in seen_uids:
            continue
        body = clean_body(message)
        prompt = (
            f"From: {message.from_}\n"
            f"Subject: {message.subject}\n"
            f"Date: {message.date}\n\n"
            f"{body}"
        )
        try:
            verdict = json.loads(ask(TRIAGE_SYSTEM, prompt, schema=TRIAGE_SCHEMA))
        except (json.JSONDecodeError, KeyError) as exc:
            if verbose:
                print(f"  ! skipped {message.uid}: {exc}", file=sys.stderr)
            continue

        record = {
            "uid": message.uid,
            "sender": message.from_,
            "subject": message.subject or "(no subject)",
            **verdict,
        }
        results.append(record)
        conn.execute(
            "INSERT OR REPLACE INTO seen "
            "(uid, processed, category, urgency, needs_reply, sender, subject, summary, action) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record["uid"],
                dt.datetime.now().isoformat(timespec="seconds"),
                record["category"],
                record["urgency"],
                int(record["needs_reply"]),
                record["sender"],
                record["subject"],
                record["summary"],
                record["action"],
            ),
        )
        conn.commit()
        if verbose:
            print(f"  [{record['urgency']}] {record['category']:<10} {record['subject'][:60]}")

    return results


# --------------------------------------------------------------------------
# Job boards
#
# Greenhouse, Lever, and Ashby expose public, documented JSON endpoints for
# each company's board. No scraping, no ToS exposure, no auth, no rate-limit
# games. Point this at the companies you actually care about.
# --------------------------------------------------------------------------

DEFAULT_BOARDS = [
    {"type": "greenhouse", "slug": "stripe"},
    {"type": "lever", "slug": "netflix"},
    {"type": "ashby", "slug": "ramp"},
]

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "rationale": {"type": "string"},
    },
    "required": ["score", "rationale"],
}


def fetch_board(board: dict) -> list[dict]:
    kind, slug = board["type"], board["slug"]
    try:
        if kind == "greenhouse":
            # content=true returns the full description; without it you get
            # titles only, which is far too thin to draft against.
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
            data = httpx.get(url, timeout=30).raise_for_status().json()
            return [
                {"company": slug, "title": j["title"],
                 "location": (j.get("location") or {}).get("name", ""),
                 "url": j["absolute_url"],
                 "description": strip_html(j.get("content", ""))[:JD_CHARS]}
                for j in data.get("jobs", [])
            ]
        if kind == "lever":
            url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            data = httpx.get(url, timeout=30).raise_for_status().json()
            return [
                {"company": slug, "title": j["text"],
                 "location": (j.get("categories") or {}).get("location", ""),
                 "url": j["hostedUrl"],
                 "description": (j.get("descriptionPlain")
                                 or strip_html(j.get("description", "")))[:JD_CHARS]}
                for j in data
            ]
        if kind == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            data = httpx.get(url, timeout=30).raise_for_status().json()
            return [
                {"company": slug, "title": j["title"],
                 "location": j.get("location", ""),
                 "url": j["jobUrl"],
                 "description": (j.get("descriptionPlain")
                                 or strip_html(j.get("descriptionHtml", "")))[:JD_CHARS]}
                for j in data.get("jobs", [])
            ]
    except httpx.HTTPError as exc:
        print(f"  ! {kind}/{slug}: {exc}", file=sys.stderr)
    return []


def jobs(conn: sqlite3.Connection, verbose: bool = True) -> list[dict]:
    boards = json.loads(BOARDS_PATH.read_text()) if BOARDS_PATH.exists() else DEFAULT_BOARDS
    resume = RESUME_PATH.read_text()[:RESUME_CHARS] if RESUME_PATH.exists() else ""
    if not resume and verbose:
        print(f"  ! no resume at {RESUME_PATH}; scoring will be weak", file=sys.stderr)

    known = {row["url"] for row in conn.execute("SELECT url FROM jobs")}
    fresh: list[dict] = []

    for board in boards:
        for posting in fetch_board(board):
            if posting["url"] in known:
                continue
            prompt = (
                f"CANDIDATE BACKGROUND:\n{resume}\n\n"
                f"POSTING: {posting['company']} — {posting['title']} "
                f"({posting['location']})\n\n{posting.get('description', '')[:3000]}"
            )
            try:
                verdict = json.loads(ask(
                    "Score 0-100 how well this posting fits the candidate. "
                    "Be harsh: 80+ means a strong, realistic match on level and "
                    "domain, not merely the same job family. A senior posting for "
                    "a mid-level candidate scores below 50 regardless of domain "
                    "overlap. rationale: one sentence naming the deciding factor.",
                    prompt,
                    schema=SCORE_SCHEMA,
                    num_ctx=16384,
                ))
            except (json.JSONDecodeError, KeyError):
                continue

            posting.update(verdict)
            fresh.append(posting)
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(url, first_seen, company, title, location, score, rationale, description) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (posting["url"], dt.datetime.now().isoformat(timespec="seconds"),
                 posting["company"], posting["title"], posting["location"],
                 posting["score"], posting["rationale"], posting.get("description", "")),
            )
            conn.commit()

    fresh.sort(key=lambda p: p["score"], reverse=True)
    if verbose:
        print(f"  {len(fresh)} new postings across {len(boards)} boards")
    return fresh


# --------------------------------------------------------------------------
# Cover letter drafting
#
# The prompt below is most of the engineering. Left unconstrained, every local
# model in this size class produces the same letter: "I am writing to express
# my keen interest in the Senior Engineer position at your dynamic company."
# The bans are specific because generic instructions ("be concise", "sound
# natural") do not survive contact with the model's priors. Naming the exact
# failure strings does.
#
# This is a scaffold, not a submission. See the checklist appended to each file.
# --------------------------------------------------------------------------

DRAFT_SYSTEM = """You draft cover letters. Output plain prose only: no headers,
no markdown, no salutation, no sign-off.

Hard rules:
- 200-280 words, three or four paragraphs.
- Open with a specific claim about the candidate's relevant experience. Never
  open with "I am writing to", "I am excited", "I am thrilled", "As a", or the
  job title.
- Every factual claim must trace to the candidate background provided. Invent
  nothing: no metrics, employers, dates, tools, or outcomes that are not there.
- Name at most two concrete requirements from the posting and tie each to
  specific prior work.
- If the candidate is thin on a stated requirement, write around it honestly.
  Never claim experience that is absent.
- Banned words and phrases: passionate, dynamic, leverage, synergy, robust,
  fast-paced, wearing many hats, delve, testament to, deeply resonates,
  perfect candidate, great fit, proven track record, results-driven,
  I look forward to hearing from you.
- No praise of the company's mission, culture, or products.
- Close with one forward-looking sentence about the work itself."""


def slugify(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:60]


def draft(conn: sqlite3.Connection, threshold: int = 70, limit: int = 5,
          verbose: bool = True) -> list[Path]:
    resume = RESUME_PATH.read_text()[:RESUME_CHARS] if RESUME_PATH.exists() else ""
    if not resume.strip():
        sys.exit(
            f"No resume at {RESUME_PATH}.\n"
            "Drafting without it yields generic filler that is worse than nothing."
        )

    already = {row["url"] for row in conn.execute("SELECT url FROM drafts")}
    candidates = [
        row for row in conn.execute(
            "SELECT * FROM jobs WHERE score >= ? ORDER BY score DESC", (threshold,)
        ) if row["url"] not in already
    ][:limit]

    if not candidates and verbose:
        print(f"  no undrafted postings scoring >= {threshold}")

    written: list[Path] = []
    for row in candidates:
        if verbose:
            print(f"  drafting {row['company']} — {row['title']} ({row['score']})")

        prompt = (
            f"CANDIDATE BACKGROUND:\n{resume}\n\n"
            f"POSTING: {row['title']} at {row['company']} ({row['location']})\n\n"
            f"{(row['description'] or '')[:JD_CHARS]}"
        )
        # Higher temperature than triage: deterministic prose in this size class
        # reads like a form letter. Still low enough to stay anchored to the resume.
        letter = ask(DRAFT_SYSTEM, prompt, temperature=0.7, num_ctx=16384).strip()

        # Strip any reasoning block or stray preamble the model emits before prose.
        letter = re.sub(r"<think>.*?</think>", "", letter, flags=re.S).strip()

        path = DRAFT_DIR / f"{slugify(row['company'])}-{slugify(row['title'])}.md"
        path.write_text(
            f"# {row['title']} — {row['company']}\n\n"
            f"- **Apply:** {row['url']}\n"
            f"- **Location:** {row['location']}\n"
            f"- **Match score:** {row['score']} — {row['rationale']}\n"
            f"- **Drafted:** {dt.date.today().isoformat()} (local {MODEL})\n\n"
            f"---\n\n{letter}\n\n---\n\n"
            "## Before sending\n\n"
            "- [ ] Verify every factual claim against your actual resume. A 14B\n"
            "      model will occasionally assert experience you do not have.\n"
            "- [ ] Add the hiring manager's name if you can find it.\n"
            "- [ ] Cut any sentence that could appear in a letter to any other\n"
            "      company. That is the test this draft most often fails.\n"
            "- [ ] Read it aloud. If it sounds like a cover letter, rewrite the\n"
            "      opening.\n"
        )
        conn.execute(
            "INSERT OR REPLACE INTO drafts (url, created, path) VALUES (?,?,?)",
            (row["url"], dt.datetime.now().isoformat(timespec="seconds"), str(path)),
        )
        conn.commit()
        written.append(path)

    return written


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def render(mail: list[dict], postings: list[dict], drafts: list[Path] | None = None) -> Path:
    today = dt.date.today().isoformat()
    lines = [f"# Brief — {today}", ""]

    urgent = sorted([m for m in mail if m["urgency"] >= 4],
                    key=lambda m: -m["urgency"])
    replies = [m for m in mail if m["needs_reply"] and m["urgency"] < 4]

    if urgent:
        lines += ["## Needs attention", ""]
        for m in urgent:
            lines.append(f"- **{m['subject']}** — {m['sender']}")
            lines.append(f"  {m['summary']}")
            if m["action"]:
                lines.append(f"  → {m['action']}")
        lines.append("")

    if replies:
        lines += ["## Awaiting reply", ""]
        lines += [f"- {m['subject']} — {m['sender']}" for m in replies]
        lines.append("")

    noise = [m for m in mail if m["urgency"] < 4 and not m["needs_reply"]]
    if noise:
        by_category: dict[str, int] = {}
        for m in noise:
            by_category[m["category"]] = by_category.get(m["category"], 0) + 1
        summary = ", ".join(f"{count} {cat}" for cat, count in sorted(by_category.items()))
        lines += ["## Low priority", "", f"{len(noise)} messages — {summary}", ""]

    strong = [p for p in postings if p["score"] >= 70]
    if strong:
        lines += ["## New postings", ""]
        for p in strong:
            lines.append(f"- **[{p['title']}]({p['url']})** — {p['company']} "
                         f"({p['location']}) · {p['score']}")
            lines.append(f"  {p['rationale']}")
        lines.append("")

    if drafts:
        lines += ["## Cover letter drafts", ""]
        lines += [f"- `{p.name}` — review before sending" for p in drafts]
        lines.append("")

    if len(lines) <= 2:
        lines.append("Nothing new.")

    path = BRIEF_DIR / f"{today}.md"
    path.write_text("\n".join(lines))
    return path


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "triage", "jobs", "draft", "run"])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--threshold", type=int, default=70,
                        help="minimum match score to draft against (default 70)")
    parser.add_argument("--limit", type=int, default=5,
                        help="max drafts per invocation (default 5)")
    args = parser.parse_args()
    verbose = not args.quiet

    conn = db()

    if args.command == "init":
        if not BOARDS_PATH.exists():
            BOARDS_PATH.write_text(json.dumps(DEFAULT_BOARDS, indent=2))
        RESUME_PATH.touch()
        print(f"Initialized {HOME}")
        print(f"  Edit {BOARDS_PATH} with companies you care about.")
        print(f"  Paste your resume into {RESUME_PATH}.")
        print(f"  security add-generic-password -s {KEYCHAIN_SERVICE} "
              f"-a <your-email> -w")
        return

    mail = triage(conn, verbose) if args.command in ("triage", "run") else []
    postings = jobs(conn, verbose) if args.command in ("jobs", "run") else []
    drafts = (draft(conn, args.threshold, args.limit, verbose)
              if args.command in ("draft", "run") else [])

    path = render(mail, postings, drafts)
    print(f"\n{path}")
    for draft_path in drafts:
        print(f"  {draft_path}")


if __name__ == "__main__":
    main()
