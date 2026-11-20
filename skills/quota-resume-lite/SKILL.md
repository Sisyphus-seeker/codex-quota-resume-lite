---
name: quota-resume-lite
description: Inspect, configure, pause or enable the installed Quota Resume Lite plugin for automatic Windows Codex quota-reset continuation. Its demo uses no real chats. Do not use this skill to control the separate codex-auto-resume plugin.
---

Resolve `../../scripts/quota_resume.py` relative to this skill directory, then invoke that
absolute path with Python 3.12+; never resolve it from the user's working directory.
For an installed copy use `<installation>/runtime/python.exe`, alongside the app directory;
the code is `<installation>/app`. Never depend on the other plugin's runtime.

- To demonstrate the scheduler, run the entrypoint with `demo`; `demo --uncertain` demonstrates
  a lost submission receipt. These commands use disposable local state and a virtual clock.
- `status` is local and reports worker state plus task counts, without message text.
- `doctor` checks the running desktop, read-only metadata schema and queue interface.
  `doctor --usage` also asks the official engine for quota; it sends no user message.
- `pause` prevents new continuations and lets the worker withdraw its own queued items.
  `resume` lifts that pause; it does not activate an installation never enabled.
- `enable` activates automatic continuation for all new quota interruptions. Use it only
  when the user requests this behavior. If the old plugin blocks activation, report the
  conflict; do not silently change the old plugin. One-time authorization is not a request
  to ask again at each quota reset. `disable` disarms sending and pauses the worker.
- `start` starts the installed worker task; `stop` requests a clean exit. Closing Codex
  is not an instruction to stop the worker; it waits while the desktop is unavailable.
- Installed commands default to their own state. For development, pass `--state` before
  the command. Never name Codex's or the old plugin's database.
- Set the global message only when requested. Read it from a file so the user's text is
  not interpolated into shell code. Status intentionally does not return its contents.

Report `queued` as accepted by Codex's queue, not as a task proven resumed. `delivered`
means a marker was found as the first user input of its own turn. An uncertain submission
is never automatically resent. The worker handles only usage-limit failures, not network
errors or arbitrary incomplete chats. It never loads a chat or starts a separate agent.

Before any first live test, identify the exact test chat and message. Do not use an
unrelated real task as a test, and do not deliberately exhaust the user's quota.
