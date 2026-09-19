# Configuration

All configuration is environment variables plus three files in `~/.mailbrief/`.
No configuration file format, no secrets in either.

## Environment variables

### Connection

| Variable | Default | Notes |
|---|---|---|
| `IMAP_USER` | *(none)* | Required. The account to read. Also the Keychain lookup key. |
| `IMAP_HOST` | `imap.gmail.com` | See provider table below. |
| `MAILBRIEF_KEYCHAIN` | `mailbrief` | Keychain service name. Change only to run multiple accounts. |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Loopback by default, deliberately. |

### Model

| Variable | Default | Notes |
|---|---|---|
| `MAILBRIEF_MODEL` | `qwen3:8b` | Any Ollama model tag. The installer suggests one based on detected RAM. |

### Volume and truncation

| Variable | Default | Notes |
|---|---|---|
| `MAILBRIEF_BODY_CHARS` | `2500` | Per-message truncation. **The primary performance lever.** |
| `MAILBRIEF_MAX_MESSAGES` | `60` | Ceiling per run. |
| `MAILBRIEF_LOOKBACK_DAYS` | `2` | Fetch window. Drop to 1 when running twice daily. |
| `MAILBRIEF_JD_CHARS` | `6000` | Job description truncation. |
| `MAILBRIEF_RESUME_CHARS` | `6000` | Resume truncation. |

### Paths

| Variable | Default | Notes |
|---|---|---|
| `MAILBRIEF_HOME` | `~/.mailbrief` | Everything lives under here. Useful for testing against a scratch directory. |

## Files

### `~/.mailbrief/resume.txt`

Plain text. Used for posting relevance scoring and as the factual basis for
cover-letter drafts.

Quality here directly determines output quality. A resume that lists
technologies without context produces scores that key on keyword overlap. One
that states scope, scale, and outcomes produces scores that discriminate by
level — which is the entire point of the scorer.

Truncated at `MAILBRIEF_RESUME_CHARS`. Put your strongest and most recent
material first.

### `~/.mailbrief/boards.json`

```json
[
  {"type": "greenhouse", "slug": "acme"},
  {"type": "lever",      "slug": "example"},
  {"type": "ashby",      "slug": "somecorp"}
]
```

`type` is one of `greenhouse`, `lever`, `ashby`. `slug` is the company identifier
in their careers URL:

| Careers URL | type | slug |
|---|---|---|
| `job-boards.greenhouse.io/acme` | `greenhouse` | `acme` |
| `jobs.lever.co/example` | `lever` | `example` |
| `jobs.ashbyhq.com/somecorp` | `ashby` | `somecorp` |

A company using a board provider not listed here, or an in-house ATS, is not
reachable without adding a branch to `fetch_board()`.

An unreachable board logs a warning and is skipped; one bad slug does not fail
the run.

### `~/.mailbrief/state.db`

SQLite. Not intended for manual editing, but queryable:

```bash
sqlite3 ~/.mailbrief/state.db \
  "SELECT score, company, title FROM jobs ORDER BY score DESC LIMIT 20;"
```

Delete it to reprocess everything from scratch. Nothing else depends on it.

## Provider settings

| Provider | `IMAP_HOST` | Authentication |
|---|---|---|
| Gmail | `imap.gmail.com` | App password; requires 2FA enabled |
| Outlook / M365 | `outlook.office365.com` | Often blocked — see below |
| Fastmail | `imap.fastmail.com` | App password |
| iCloud | `imap.mail.me.com` | App-specific password |

**Microsoft 365**: many tenants have disabled IMAP basic authentication entirely.
If yours has, this credential path is closed and no app password will work — the
alternative is OAuth via MSAL, which is not implemented here.

## Command-line flags

```
mailbrief.py {init,triage,jobs,draft,run} [--quiet] [--threshold N] [--limit N]
```

| Flag | Default | Applies to |
|---|---|---|
| `--quiet` | off | All. Suppresses per-item progress; errors still print. |
| `--threshold` | `70` | `draft`, `run`. Minimum posting score to draft against. |
| `--limit` | `5` | `draft`, `run`. Maximum drafts generated per invocation. |

`--limit` exists because drafting is the slowest operation in the tool. Five
letters at 16k context is minutes of prefill on a passively cooled machine.

## Multiple accounts

Run separate state directories and Keychain entries:

```bash
security add-generic-password -s mailbrief-work -a you@work.com -w

MAILBRIEF_HOME=~/.mailbrief-work \
MAILBRIEF_KEYCHAIN=mailbrief-work \
IMAP_USER=you@work.com \
IMAP_HOST=outlook.office365.com \
  ~/.mailbrief/venv/bin/python ~/.mailbrief/mailbrief.py triage
```

Each gets its own `state.db`, briefs, and drafts. Duplicate the LaunchAgent with
a distinct `Label` to schedule both.
