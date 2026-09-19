# Architecture

## Design premise

The workload is a fixed pipeline, not an agent task:

```
fetch ──▶ classify ──▶ summarize ──▶ render
```

Every step is known in advance. Nothing about message N changes what happens to
message N+1. There is no plan to derive, so there is nothing for an agent loop to
contribute except latency, token cost, and failure modes.

This matters more than it sounds. A hosted coding assistant re-derives the plan
on every run and bills for it. A deterministic pipeline that calls a model only
for the genuinely fuzzy sub-problems — *what kind of message is this*, *say this
in one sentence* — does the same job at zero marginal cost.

It also determines what model class is viable. An 8–14B local model is reliable
at single-turn classification against a constrained output space and unreliable
at multi-step tool use. The architecture only ever asks for the former.

## Components

### `ask()` — the only model entry point

One function issues every inference call. It takes a system prompt, a user
prompt, an optional JSON Schema, a temperature, and a context size.

Passing a schema as Ollama's `format` parameter constrains decoding, so output is
guaranteed-parseable JSON rather than prose that usually contains JSON. This is
load-bearing: without it, small models intermittently wrap their output in
explanation and you end up maintaining regex salvage code. With it, the only
remaining failure mode is a wrong label, which is cheap and recoverable when the
label space has seven members.

Temperature is deliberately split. Triage and scoring run at 0.2 — these are
classification tasks where determinism is correct. Drafting runs at 0.7, because
deterministic prose in this size class reads like a form letter.

### `triage()` — mailbox → classified records

Fetches with `mark_seen=False` (see [SECURITY.md](SECURITY.md)), skips UIDs
already in `state.db`, and makes one schema-constrained call per message.

`clean_body()` does the work that determines runtime. It prefers the plaintext
part, falls back to stripped HTML, cuts quoted reply chains at the `On ... wrote:`
boundary, collapses tracking URLs, and truncates hard. On Apple Silicon, prompt
processing dominates — so this function is a bigger performance lever than model
selection.

### `jobs()` — public boards → scored postings

Three providers, each a documented unauthenticated JSON endpoint:

| Provider | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` |

`content=true` on Greenhouse is required; without it you get titles only, which
is too thin to score or draft against. Greenhouse returns entity-encoded HTML,
which is why `strip_html()` unescapes twice.

Scoring is deliberately harsh — the prompt states that a senior posting for a
mid-level candidate scores below 50 regardless of domain overlap. A scorer that
rates everything 75 is worse than no scorer, because it costs you the time it
was supposed to save.

### `draft()` — scored postings → cover letters

Selects postings above a score threshold that have no draft yet, and generates
one letter each.

The prompt is most of the engineering here. Left unconstrained, every model in
this size class produces the same letter, opening with "I am writing to express
my keen interest in." The banned-phrase list is specific because generic
instructions — "be concise", "sound natural" — do not survive contact with the
model's priors. Naming the exact failure strings does.

Output is written with a verification checklist as its first section, because the
draft is a scaffold and treating it as a finished artifact is the failure mode
this design most needs to prevent.

### `render()` — records → brief

Pure function over the data. Groups by urgency, then by needs-reply, then
collapses everything else into a count by category. No model call.

## State

SQLite at `~/.mailbrief/state.db`.

| Table | Key | Purpose |
|---|---|---|
| `seen` | message UID | Triage results; prevents reprocessing |
| `jobs` | posting URL | Scored postings; prevents rescoring |
| `drafts` | posting URL | Which postings have a letter already |

Every write commits immediately, before the next network call. A crash mid-run
therefore loses at most the in-flight item.

Schema changes are handled additively — `db()` checks `PRAGMA table_info` and
issues `ALTER TABLE` for columns added after a user's database was created,
rather than requiring a wipe.

### UID stability

IMAP UIDs are unique only within a folder's `UIDVALIDITY` epoch. If a provider
resets it, previously-seen UIDs may be reissued and the dedup assumption breaks.
The recovery is to delete `state.db`; the cost is one redundant run. This is an
acceptable trade against the complexity of tracking `UIDVALIDITY` and
`Message-ID` as a composite key.

## Scheduling

A launchd `StartCalendarInterval` agent, not cron.

On a laptop this is not a style preference. cron silently skips jobs scheduled
while the machine is asleep; launchd fires once on the next wake. For a
twice-daily brief on a machine that spends nights closed, cron means the job
frequently never runs at all.

launchd does not expand `~`, does not source your shell profile, and runs with a
minimal environment. Every path in the plist must therefore be absolute and every
variable explicit — which is why `install.sh` generates the plist at install time
rather than shipping a template with placeholders.

## Extension points

Deliberately left open:

- **Additional board providers** — add a branch to `fetch_board()` returning the
  same dict shape.
- **Escalation to a frontier model** — the natural hybrid. Run everything local,
  route only `urgency >= 4` messages to a hosted API for a better summary. A few
  cents a day instead of per-request billing on everything.
- **Reply drafting** — the same pattern as `draft()`.

Deliberately closed: anything that writes to the mailbox. See
[SECURITY.md](SECURITY.md) for why that boundary is doing real work.
