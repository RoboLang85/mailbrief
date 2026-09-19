# mailbrief

Local-first email triage and job-posting digest. Classification and drafting run
against [Ollama](https://ollama.com) on your own machine — no message content
reaches a third-party API, and no per-request billing.

Built for the case where a commercial coding assistant is being burned on work
that is structurally a fixed pipeline, not an agent loop.

```
                    ┌──────────────┐
   IMAP (read-only) │              │  markdown brief
  ──────────────────▶   mailbrief  ├──────────────────▶  ~/.mailbrief/briefs/
   Greenhouse/Lever │              │  cover-letter drafts
   /Ashby JSON APIs │              ├──────────────────▶  ~/.mailbrief/drafts/
  ──────────────────▶              │
                    └───────┬──────┘
                            │ HTTP :11434
                    ┌───────▼──────┐
                    │    Ollama    │   local inference, no egress
                    └──────────────┘
```

## What it does

| Command | Behavior |
|---|---|
| `init` | Create `~/.mailbrief`, the SQLite state DB, and starter config. |
| `triage` | Fetch recent mail, classify and summarize each message, write a brief. |
| `jobs` | Pull public job boards, score each posting against your resume. |
| `draft` | Generate cover-letter drafts for high-scoring postings. |
| `run` | All three, into one brief. |

Output is a dated markdown file: what needs attention today, what is waiting on
a reply, a count of everything low-priority, new postings worth a look, and any
drafts generated.

## Why not an agent framework

This workload is a fixed pipeline — fetch, classify, summarize, render. There is
no planning step, so an agent loop contributes latency and failure modes and
nothing else. Two schema-constrained model calls per message beat a ReAct loop on
every axis that matters here.

That choice is also what makes small local models viable. A 8–14B model is
reliable at "answer this one question about this one input" and unreliable at
multi-step tool use. The architecture only ever asks it to do the former.

## Requirements

- macOS on Apple Silicon (the installer is macOS-specific; the Python runs anywhere)
- Python 3.10+
- [Ollama](https://ollama.com)
- An IMAP mailbox and an app password

## Install

```bash
git clone https://github.com/RoboLang85/mailbrief.git
cd mailbrief
bash install.sh
```

The installer is idempotent. It detects available RAM and warns if your model
choice is a poor fit, locates Homebrew across all common prefixes, installs and
starts Ollama only if absent, pulls the model, builds a virtualenv, and generates
a LaunchAgent plist with correct absolute paths.

It deliberately does **not** load the LaunchAgent. Verify a manual run first — an
unverified scheduled job fails silently into a log twice a day.

### Credentials

Gmail requires an app password; your account password will not work over IMAP,
and 2FA must be enabled to generate one.

```bash
security add-generic-password -s mailbrief -a you@example.com -w
```

`-w` prompts for the value. It is not echoed and does not enter shell history.
The secret stays in the login keychain — this project never reads credentials
from a dotfile or environment variable.

### Configure

```bash
$EDITOR ~/.mailbrief/resume.txt     # plain text, used for posting relevance
$EDITOR ~/.mailbrief/boards.json    # companies to watch
```

`boards.json` is a list of `{"type": "greenhouse"|"lever"|"ashby", "slug": "..."}`.
The slug is the company identifier in their careers URL — for
`job-boards.greenhouse.io/acme`, the slug is `acme`.

### Run

```bash
export IMAP_USER=you@example.com
~/.mailbrief/venv/bin/python ~/.mailbrief/mailbrief.py run
```

Once that works, enable the schedule:

```bash
launchctl load ~/Library/LaunchAgents/com.user.mailbrief.plist
```

## Model selection

| Unified memory | Model | Resident |
|---|---|---|
| 16 GB | `qwen3:8b` | ~5 GB |
| 24–32 GB | `qwen3:14b` | ~9 GB |
| 32 GB+ | `qwen3:30b-a3b` | ~18.6 GB |

`qwen3:30b-a3b` is a mixture-of-experts model with roughly 3B active parameters,
so it generates faster than its footprint suggests. On 24 GB it fits only with
the Metal allocation cap raised and other applications closed:

```bash
sudo sysctl iogpu.wired_limit_mb=20480   # resets on reboot
```

For a seven-label classification task this buys very little. Start at 14B.

## Performance

Prompt processing dominates runtime, not token generation. Sixty emails at 2,500
characters each is roughly 40k tokens of prefill.

Tuning levers, in order of effect:

1. `MAILBRIEF_BODY_CHARS` — drop to 1500. Triage rarely needs more than the first
   screenful of a message.
2. `MAILBRIEF_LOOKBACK_DAYS=1` — once running twice daily.
3. Model size — the last lever, not the first.

On a passively cooled machine (MacBook Air), sustained runs will thermally
throttle. Prefer truncation over a smaller model.

## Scope boundary: job applications

This tool finds, scores, and drafts. It does not submit.

Greenhouse, Lever, and Ashby expose documented, unauthenticated JSON endpoints
per company board. Consuming those is what they are for.

Automating LinkedIn is a different matter. Driving a logged-in session with
Playwright or Selenium violates their User Agreement's prohibition on automated
access, their bot detection is effective, and the realistic outcome is account
restriction. When LinkedIn is also your professional network and recruiter
contact surface, that trade is poor. There is no official API for job submission.

The workflow this supports: the tool surfaces matches and drafts a starting
point; a human reads it and presses submit.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — data flow, schema, design rationale
- [Configuration](docs/CONFIGURATION.md) — every environment variable and file
- [Security model](docs/SECURITY.md) — threat model, trust boundaries, residual risk
- [Troubleshooting](docs/TROUBLESHOOTING.md) — failure modes and diagnosis

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
