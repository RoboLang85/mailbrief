# Contributing

## Scope

This is a personal-scale tool with a deliberately narrow remit. Changes that fit:

- Additional job-board providers
- Additional IMAP provider quirks and documentation
- Prompt improvements backed by before/after output on real messages
- Performance work on the truncation and prefill path
- Bug fixes

Changes that do not fit, and why:

**Anything that writes to the mailbox.** Send, delete, archive, move, auto-reply.
The read-only constraint is what makes prompt injection via email body an
accepted risk rather than a real attack — an attacker controls text that reaches
the model, and today the worst outcome is a misleading line in a markdown file.
Adding a write path makes untrusted content the steering input to a privileged
action. See [docs/SECURITY.md](docs/SECURITY.md).

**Automated application submission.** Specifically LinkedIn. It violates their
User Agreement, their detection is effective, and the downside is losing the
account. This boundary is not negotiable in this repo; fork it if you disagree.

**An agent framework.** The workload is a fixed pipeline. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the reasoning. A PR that adds
one needs to argue against that section, not just around it.

## Development

```bash
git clone https://github.com/RoboLang85/mailbrief.git
cd mailbrief
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Run against a scratch state directory so you never touch real data:

```bash
MAILBRIEF_HOME=/tmp/mailbrief-dev \
IMAP_USER=you@example.com \
  ./venv/bin/python mailbrief.py triage
```

## Testing prompt changes

Prompt changes need evidence, because they are the easiest thing in the project
to make worse while believing you improved it.

1. Keep a fixed set of messages. `state.db` already holds prior classifications.
2. Change the prompt.
3. Delete only the affected rows and re-run:
   ```bash
   sqlite3 /tmp/mailbrief-dev/state.db "DELETE FROM seen;"
   ```
4. Compare the urgency distribution before and after:
   ```bash
   sqlite3 /tmp/mailbrief-dev/state.db \
     "SELECT urgency, COUNT(*) FROM seen GROUP BY urgency ORDER BY urgency;"
   ```

State which model you tested against. A prompt tuned on `qwen3:30b-a3b` often
regresses on `qwen3:8b`, and 8B is the floor this project supports.

## Style

- Standard library first. Two runtime dependencies is the budget; adding a third
  needs justification in the PR.
- Comments explain *why*, not *what*. The existing comments are the standard:
  they record the reasoning that is not recoverable from the code.
- No secrets in code, config files, or environment variables. Credentials come
  from the OS keychain.
- Every new network call gets a timeout and an exception handler that degrades
  rather than aborting the run.

## Pull requests

State what you changed, why, and what you ran it against. Include before/after
output for anything touching prompts or classification.

Never include real message content, sender addresses, or credentials in an
issue, a PR, or a test fixture.
