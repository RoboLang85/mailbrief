# Troubleshooting

Failure modes in rough order of how often you will hit them.

## Python

### `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`

The venv is on Python 3.9 and you are running a build that predates the
`from __future__ import annotations` line. PEP 604 unions (`dict | None`) in a
function signature are evaluated when the function is defined, and 3.9 cannot
evaluate them.

Current versions support 3.9. If you hit this, you are on an old `mailbrief.py`:

```bash
git -C /path/to/mailbrief pull
cp /path/to/mailbrief/mailbrief.py ~/.mailbrief/mailbrief.py
```

To rebuild the venv on a newer interpreter instead:

```bash
rm -rf ~/.mailbrief/venv
bash install.sh      # picks the newest python3.x on PATH
```

Check what the venv is actually running:

```bash
~/.mailbrief/venv/bin/python --version
```

Note that `/usr/bin/python3` on macOS is 3.9 and is what a bare `python3 -m venv`
will use unless a newer interpreter comes first on `PATH`.

## Ollama

### `Cannot reach Ollama at http://127.0.0.1:11434`

The server is not running.

```bash
curl -s http://127.0.0.1:11434/api/tags | head -c 200
```

Nothing back means start it:

```bash
brew services start ollama    # if installed via Homebrew
ollama serve                  # foreground, for debugging
```

If you installed the Ollama .app rather than the Homebrew formula, the server
runs only while the app is open. That is a common cause of a scheduled run
failing at 07:15 while the same command works by hand at noon. Prefer the
Homebrew service for scheduled use.

### Model not found

```bash
ollama list
ollama pull qwen3:14b
```

Tag names are exact. `qwen3:14b` and `qwen3-14b` are not the same string, and the
error will simply say the model is missing.

### Runs are extremely slow

Expected on first invocation — the model has to load into memory. Subsequent runs
within Ollama's keepalive window are much faster.

If every run is slow:

1. Lower `MAILBRIEF_BODY_CHARS` to 1500. Prefill dominates; this is the largest
   lever by a wide margin.
2. Lower `MAILBRIEF_LOOKBACK_DAYS` to 1.
3. Only then consider a smaller model.

On a passively cooled machine, check whether you are thermally throttled:

```bash
sudo powermetrics --samplers smc -n 1 -i 1 | grep -i thermal
```

### Memory pressure or swapping

A model that does not fit forces swap and the run becomes unusable rather than
merely slow.

```bash
memory_pressure | tail -5
```

Drop to a smaller model. For `qwen3:30b-a3b` on 24 GB specifically, you need the
Metal cap raised and other applications closed:

```bash
sudo sysctl iogpu.wired_limit_mb=20480   # resets on reboot
```

## IMAP

### `No Keychain entry for service=mailbrief account=...`

The entry does not exist, or `IMAP_USER` does not match the account it was stored
under. They must match exactly.

```bash
security find-generic-password -s mailbrief -a you@example.com    # omit -w to check existence
security add-generic-password -s mailbrief -a you@example.com -w  # create
```

### `AUTHENTICATIONFAILED`

In order of likelihood:

1. You used your account password rather than an app password. Gmail rejects the
   former over IMAP unconditionally.
2. 2FA is not enabled, so no app password could have been generated.
3. The app password was revoked — they do not expire, but a password change
   invalidates all of them.
4. IMAP is disabled in the provider's settings.
5. **Microsoft 365 only**: the tenant has disabled IMAP basic auth entirely. No
   app password will work. This path is closed; OAuth via MSAL is the
   alternative and is not implemented here.

### Nothing is returned, but the mailbox has mail

`LOOKBACK_DAYS` is a date filter, not a count. If nothing arrived in the window,
nothing is fetched. Widen it:

```bash
MAILBRIEF_LOOKBACK_DAYS=7 ~/.mailbrief/venv/bin/python ~/.mailbrief/mailbrief.py triage
```

Also check whether the messages were already processed — the brief only shows new
records:

```bash
sqlite3 ~/.mailbrief/state.db "SELECT COUNT(*) FROM seen;"
```

### Everything is reprocessed on every run

The provider reset `UIDVALIDITY`, invalidating stored UIDs. Delete `state.db` and
accept one duplicate run.

## Scheduling

### The LaunchAgent never fires

Check it is actually loaded:

```bash
launchctl list | grep mailbrief
```

The second column is the last exit status. `0` is clean; anything else means the
job ran and failed.

```bash
tail -20 ~/.mailbrief/error.log
```

### It works by hand but fails when scheduled

Almost always environment. launchd does not source your shell profile and runs
with a minimal `PATH`.

- Every path in the plist must be absolute. `~` is not expanded.
- `IMAP_USER` must be set in the plist's `EnvironmentVariables`, not inherited.
- Ollama must be running as a service, not as a foreground process you started in
  a terminal that has since closed.

Reproduce the scheduled environment exactly:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin \
  ~/.mailbrief/venv/bin/python ~/.mailbrief/mailbrief.py run
```

If that fails and a normal invocation succeeds, the difference is environment.

### Keychain access fails only under launchd

A LaunchAgent runs in your GUI session and can normally read the login keychain.
If the keychain is locked — some FileVault and auto-login configurations leave it
locked until first manual unlock — `security` will fail.

Symptom: the job succeeds after you have logged in and used the machine, fails on
the first run after a reboot.

### After editing the plist

launchd caches the loaded definition. Reload it:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.mailbrief.plist
launchctl load   ~/Library/LaunchAgents/com.user.mailbrief.plist
```

Validate syntax before loading — a malformed plist fails silently:

```bash
plutil -lint ~/Library/LaunchAgents/com.user.mailbrief.plist
```

## Output quality

### Everything is classified urgency 1, or everything is urgency 5

Small models drift on calibration. Check the distribution:

```bash
sqlite3 ~/.mailbrief/state.db \
  "SELECT urgency, COUNT(*) FROM seen GROUP BY urgency ORDER BY urgency;"
```

A healthy inbox skews low. If everything is a 4 or 5, the model is agreeing with
subject-line urgency — step up a model size before rewriting the prompt.

### Job scores cluster at 70–80 with no discrimination

Usually a thin `resume.txt`. A resume listing technologies without scope gives
the scorer nothing but keyword overlap to work with. Add scale, seniority, and
outcomes.

Also check truncation — if your resume exceeds `MAILBRIEF_RESUME_CHARS`, the
scorer never sees the end of it.

### Drafts read like form letters

Expected at low temperature; `draft()` uses 0.7 for this reason. If they still
read generically:

1. The posting had no description. Confirm:
   ```bash
   sqlite3 ~/.mailbrief/state.db \
     "SELECT company, title, LENGTH(description) FROM jobs ORDER BY score DESC LIMIT 10;"
   ```
   Zero or near-zero length means the board returned titles only and there was
   nothing to tailor against.
2. The model is too small. Drafting is the one task here that genuinely benefits
   from more parameters — unlike triage, where 8B is sufficient.

### A draft claims experience you do not have

Known failure mode, documented in [SECURITY.md](SECURITY.md). The prompt forbids
it and the model sometimes does it anyway. This is why every draft ships with a
verification checklist. Read it before sending.

## Diagnostics

```bash
# Ollama reachable and which models are present
curl -s http://127.0.0.1:11434/api/tags | python3 -m json.tool

# Single model call, no mailbox involved
curl -s http://127.0.0.1:11434/api/chat -d '{
  "model":"qwen3:14b","stream":false,
  "messages":[{"role":"user","content":"Reply with the word OK."}]}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["message"]["content"])'

# Board endpoint reachable, independent of the tool
curl -s "https://boards-api.greenhouse.io/v1/boards/YOURSLUG/jobs" \
  | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["jobs"]), "postings")'

# State overview
sqlite3 ~/.mailbrief/state.db \
  "SELECT 'seen', COUNT(*) FROM seen
   UNION ALL SELECT 'jobs', COUNT(*) FROM jobs
   UNION ALL SELECT 'drafts', COUNT(*) FROM drafts;"
```

Isolate by running stages separately — `triage` and `jobs` share only the model,
so a failure in one localizes the fault immediately.
