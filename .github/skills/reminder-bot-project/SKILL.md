---
name: reminder-bot-project
description: "Use when developing, reviewing, testing, or debugging any part of reminder_bot: Telegram handlers and service menus, group or personal reminders, Excel/Yandex Disk schedules, SQLite registry, backups, deployment, or the static Web App."
user-invocable: true
---

# reminder_bot project workflow

Use this skill for project-wide work in `tunmonitor/reminder_bot`, including
code changes, reviews, tests, operational diagnosis, and documentation.
Project requirements and examples are also recorded in `README.md`, `TZ2.txt`,
and `UI_SKETCHES.md` (v1, current); check the relevant section when behavior
is ambiguous. Plans: `PLAN.md` (customer decisions, stage status),
`EXECUTION_PLAN.md` (technical tasks), `BACKLOG.md`, `UI_SKETCHES_V2.md`
(menu redesign proposal, not approved until the user says so).

This file is the single source for Claude Code and Codex/Copilot:
`.github/skills/` is the real directory; `.claude/skills` and
`.agents/skills` are symlinks to it. Edit only `.github/skills/...`.
Reply to the user in Russian; identifiers, paths and commands stay as is.

## Environments: Mac (dev) vs Raspberry Pi (test, prod)

Detect where you are first: `uname -s` → `Darwin` = Mac dev checkout,
`Linux` + `/home/pi` = Raspberry Pi. Below, `$PY` is the interpreter of the
current environment.

| | Mac (development) | Raspberry Pi (test / prod) |
|---|---|---|
| Checkout | `/Users/alex/projects/app/reminder_bot/dev`, branch `dev` | `/mnt/external_data/reminder_bot/test`, prod in its own directory |
| `$PY` | `/Users/alex/projects/.venv/bin/python` (shared venv; never create a new one; install with `$PY -m pip install -r requirements.txt`) | `/home/pi/.venv/bin/python` |
| Temp files, test `TMPDIR`, scratch worktrees | `/Users/alex/projects/.tmp/reminder_bot` (not `/tmp`, not the project folder) | `/dev/shm/app/reminder_bot/src/.tmp` (RAM, not the SD card) |
| Diagnostic workbooks/logs | `/Users/alex/projects/.tmp/reminder_bot/debug/` (dir 700, files 600) | `/dev/shm/reminder_bot/debug/` (dir 700, files 600) |
| Role | write code, run tests, open PRs | run the bot (test/prod); code arrives only through GitHub |
| Bot process | never run with the test/prod `BOT_TOKEN` (two pollers on one token conflict); a local run needs a separate dev bot token | `manage.sh` / cron watchdog; do not stop, restart or deploy without an explicit request |
| Tests | `TMPDIR=… $PY -m unittest discover -s tests -q > <tmp>/full.log 2>&1` in the background; some tests are skipped on macOS (POSIX-only) | run in the background too: a heavy foreground run can be killed (exit 137) |

Mac specifics:

- No Telegram sends in tests, dry-runs or previews. No `ssh`/`scp` to the Pi
  to copy code or change the live environments (read-only look is fine when
  the user asks).
- Never print tokens, `*.env` files, full participant records or private
  messages.
- Delete only temp files you created yourself.

Raspberry Pi specifics:

- Test and prod have separate directories, configs, databases and tokens.
- `.github/workflows/deploy.yml` runs on the self-hosted runner
  `[self-hosted, tunmonitor]`: a push to a PR with head `dev` and base `test`
  deploys to **test**; a push to `main` deploys to **prod**. A PR from any
  other head into `test` does not deploy.
- Never edit files on the Pi by hand; fix in `dev` and ship through a PR.

## Git flow and the local `dev` branch

- Work and commit **only on `dev`**; `test` and `main` change only through PRs.
  Never force-push, never push to `test`/`main` directly.
- `dev` also receives PRs from other agents (`copilot/*` → `dev`, e.g. #91,
  #92). Before starting work run `git fetch` and `git status -sb`; if `dev` is
  behind `origin/dev`, merge it (`git merge origin/dev`) **before** editing, so
  docs such as `EXECUTION_PLAN.md` are not overwritten.
- Flow: `dev` → PR to `test` (deploys to test) → comment `/review` after the
  PR is opened and after every push → wait for greptile
  (`/Users/alex/projects/bin/greptile_watch.sh <PR> <ISO time before push>` in
  the background; one updating comment «Confidence Score: N/5» plus line
  comments) → fix findings, repeat until 5/5 with no new findings → merge to
  `test` → give the user a checklist of buttons/screens to verify in the test
  bot. The `test` → `main` PR is merged **only by the user**.
- Before every commit or push: review the diff by a separate model (Agent,
  `model: "sonnet"`), correctness bugs only, each with `file:line` and a failure
  scenario; verify findings against the code, fix the real ones, tell the user
  what was found and fixed.
- Commit only when the user asks. Do not open new PRs or start new PLAN steps
  while the user has paused them (check project memory).

## UI principles (every screen you add or change)

- **Friendly to the user.** Users are service admins and church members, not
  engineers: plain words («за день до воскресенья», not «offset»/«дн.»), each
  screen says what it is and what to do next, errors give the reason and the
  next action, empty states invite action, dangerous actions are confirmed,
  manual input shows an example and has quick buttons, never ask for IDs.
- **Clear menu hierarchy.** One meaning — one place; sections named by the
  user's task; frequent actions ≤ 3 taps from `/start`; the path is visible
  («ЗВУК › 📅 График › Замены»); «back» is named and behaves the same
  everywhere; same-meaning buttons have the same label; ≤ 6 buttons per screen
  without paging, 2–3 per row; dangerous actions in a separate bottom row.
- Check `UI_SKETCHES.md` (and `UI_SKETCHES_V2.md` once approved) and update
  the sketches and `docs/ADMIN_GUIDE.md` / `docs/VOLUNTEER_GUIDE.md` in the
  same PR.

## Safety and working-tree rules

- Run project Python commands with `$PY` (see the environment table).
- Put temporary files (test `TMPDIR`, scratch worktrees) in the temp folder of
  the current environment (table above).
- Start by checking `git status --short`. Preserve existing user changes; never
  revert unrelated edits or commit/push unless asked.
- Never print `BOT_TOKEN`, credential files, full participant records, or
  private message contents. Debug logs and schedule workbooks may contain names,
  usernames, Telegram IDs, assignments, and rendered message text; treat them
  as private.
- Do not send Telegram messages during tests, dry-runs, or previews. Do not
  invoke live delivery paths to validate rendering.
- Do not delete original workbooks or broad directories. Keep diagnostic
  workbooks/logs in the debug folder of the current environment (table above)
  with directory mode 700 and file mode 600; these artifacts are not
  automatically removed.
- Do not stop, restart, deploy, restore, or mutate a live environment unless
  explicitly requested. For restore operations, the bot must be stopped and
  the snapshot environment must match the target.
- Prefer focused unit tests first, then the complete suite when a code change
  is ready. Report warnings separately from test failures.

## Project map

- `reminder_bot.py`, `bot/reminder_bot.py`, `bot/app.py` — application entry
  points, lifecycle, dependency wiring, scheduling, and notification flows.
- `bot/handlers.py` — Telegram commands, callback handlers, access checks,
  conversation state, service setup, and personal notification screens.
- `bot/service_registry.py` — SQLite schema, migrations, settings, contacts,
  reminder schedules, delivery records, and configuration audit.
- `bot/excel_parser.py`, `bot/yandex_disk.py` — workbook download, schedule
  parsing, aliases, contacts, and assignment resolution.
- `bot/presentation.py`, `bot/notifier.py` — shared assignment/message
  formatting, sending, delivery handling, and administrator warnings.
- `bot/commands.py` — role resolution and per-role Telegram command menus.
- `bot/scheduler.py` — cron jobs, reminder occurrence/date rules, and refresh.
- `bot/state.py` — persisted delivery/confirmation state cache.
- `bot/ui.py` — shared Telegram screen rendering, rich-text markers
  (`code()`, `user_link()`, `plain_text()`, `visible_units()`), button
  layout, pagination, callback limits, and menu cleanup.
- `bot/backup.py`, `bot/backup_cli.py`, `bot/secure_fs.py` — private,
  validated snapshots, restore, filesystem safety, and instance locking.
- `bot/webapp_server.py`, `webapp/` — static Web App only; there is no data API.
- `bot/debug_schedule.py` — private Excel schedule dry-run without delivery.
- `tests/` — `unittest` suite; `.github/workflows/deploy.yml` runs it in CI
  before eligible deployments.

## Development procedure

1. Read the relevant implementation and tests, then check `README.md`,
   `TZ2.txt`, or `UI_SKETCHES.md` for project-specific behavior.
2. Trace the full affected path (entry point, callback/message handler,
   registry, scheduler/notifier, and UI as applicable). Reuse shared helpers
   instead of adding parallel logic.
3. Make a surgical change that preserves permissions, callback formats,
   confirmation steps, delivery idempotency, and user-visible behavior unless
   the task explicitly changes them. Surface failures through existing logs,
   administrator notifications, or recoverable UI; do not silently fall back.
4. Add or update focused tests for both successful behavior and relevant edge
   cases. Use mocks/fakes for Telegram, Yandex Disk, clocks, and database
   failures; do not send messages to real users.
5. Run focused tests, then (for code changes) the full suite and syntax/diff
   checks:

   ```bash
   # $PY and <tmp> — from the environment table; TMPDIR keeps temp files there
   TMPDIR=<tmp> $PY -m unittest tests.test_<area> -v
   TMPDIR=<tmp> $PY -m unittest discover -s tests -q > <tmp>/full.log 2>&1   # background
   $PY -m compileall -q bot tests
   git diff --check
   ```

   Choose existing test modules relevant to the change; do not literally run
   the `<area>` placeholder. The full run writes to a file in the temp folder
   and goes to the background. Documentation-only changes need no tests unless
   documentation tests exist.

## Domain rules and invariants

### Excel schedule and contacts

- Monthly sheets are named with a Russian month and two-digit year, e.g.
  `ОКТЯБРЬ26`. Schedule dates are in column A; task names and assignees are in
  columns A and B. The `Списки` sheet contains task headers in A and contact
  name, username, and Telegram ID in A, B, and C (legacy assembly parser
  `parse_people`).
- `parse_service_rosters` reads `Списки` for all services: row 1 holds service
  headers (block runs right to the next header), task cells start a task in
  their column, people below. Username/ID cells are skipped, never read:
  Telegram binding happens only in the bot. A person is identified by name
  within a service (names must be unique; a repeated name in one task is an
  issue). `ServiceRegistry.sync_service_rosters` stores names and tasks in
  `service_members` (writes only on real changes,
  because any write triggers a backup and job refresh). `_load_service_task_
  assignments(..., registry=...)` syncs rosters and `ensure_member_contacts`
  on every schedule read; roster failures are logged and never block
  reminders. Binding UI is person-centric (`bind_service_member` covers all
  the person's tasks); `members-v1` migrates old contacts once.
- Chat topics: `/here [service]` in a group topic stores
  `services.group_thread_id` (`set_group_thread`, service editor, chat must
  be the bound one; `set_group_chat` to another chat resets it). Every send
  to a service group passes `**thread_kwargs(service)`.
- Services, tasks, and month-sheet columns come from
  `reminder_bot_services.json` (`bot/services.py` validates it at import).
  `parse_service_events(..., services)` maps column headers to services,
  merges several columns of one service into one event per date, renames
  tasks to catalog names via aliases (`normalized_name`), silently drops
  disabled tasks (and columns whose fixed task is disabled), and reports
  unknown tasks in `ServiceEvent.issues`. Pass
  `registry.service_catalog()` so super-admin task toggles apply.
- `ServiceRegistry.initialize` syncs the catalog into `services` /
  `service_tasks`; services missing from the file are archived, never
  deleted, and `list_services` hides them. Task enable state and
  admin-added tasks live only in SQLite; admin tasks are renumbered after
  catalog tasks so `position` stays a unique button id. Migration
  `catalog-v1` runs once: it moves admins, contacts, personal texts (with
  rules) and prefs of slides/rehearsal/coffee into broadcast/sound/kitchen
  and renames stored task names via `_move_contact` / `_move_user_text`,
  which never drop another person's binding or a user's rules. User-facing
  lists (`list_user_services`, notification state, bulk toggle) skip
  archived services.
- The legacy assembly role flow (`parse_schedule`/`parse_people`) keeps its
  own aliases: `ХКЦ` → `Ответственный`; `Ведущий`/`Ведет` → `Ведущий`;
  `Проповедует`/`Проповедет` → `Проповедник`.
- Rows without a task continue the previous task until an empty name cell,
  another task, a marker, or a date; before the first task they belong to
  the column task or the service. A row starting with `КОММЕНТАРИИ` and the
  first Sunday of another month end the sheet. A column `task` applies to
  every untasked row (also after markers); `default_task` covers untasked
  rows at the start of a date block (kitchen/order → «в смене»).
- Contacts are matched by exact (task, name); do not add name-only
  fallbacks across tasks — namesakes in one service would get each other's
  mentions and personal reminders. `_move_user_text` moves a text only
  when the user's binding moved to the target task.
- Informational tasks may be marked with a leading `*`, `[...]`, or `[*]`.
  They remain available to group templates as `{informational_notes}`, but
  are excluded from responsible-contact selection, personal reminders, and
  `{task_assignments}`. A group reminder with only informational tasks is not
  sent.
- A missing individual assignment/contact/valid positive Telegram ID should
  be reported without preventing valid recipients or the group from receiving
  their messages. Workbook download/read failures are fatal for that run.
- For safe diagnosis, use the dry-run rather than sender paths:

  ```bash
  $PY -m bot.debug_schedule --date YYYY-MM-DD
  ```

  Omit `--date` to use the next target Sunday. Preserve downloaded files when
  diagnosing parser failures; inspect workbook structure without dumping
  participant cell values. Known quirks include incorrect worksheet dimension
  `A1` and invalid border style `solid`; parser recovery must not modify the
  original workbook.

### Group and personal reminders

- Group reminders are independent records with their own `reminder_days`,
  `reminder_time`, enabled state, and optional body. `body = NULL` resolves
  through the service default template and then the built-in default.
- Templates must render through the same resolver/value mapping as real
  delivery. Unknown legacy fields remain literal when the production renderer
  supports that behavior. Supported group fields include `{service_name}`,
  `{sunday_date}`, `{task_assignments}`, and `{informational_notes}`.
- `next_reminder_occurrence(now, reminder_days, reminder_time)` selects the
  earliest Sunday whose send date/time has not passed. Send date is event
  Sunday minus `reminder_days`; a passed send time selects the next eligible
  Sunday. This includes a zero-day Sunday reminder before its configured time.
  Use the shared helper for cards and previews; do not reimplement date math.
- Preview must not deliver messages. It displays event date separately from
  send date/time, rejects informational-only schedules, and catches workbook
  parsing failures with a logged, navigable error screen.
- Callback-driven asynchronous work must validate the active session, screen,
  item ID, and request-specific token before loading and after every relevant
  `await`. New previews invalidate old requests; old results must not replace
  a newer menu.
- Changing a group reminder's days/time (`update_group_reminder`,
  `slot_changed`) deletes its `group:%` delivery marks for event dates from yesterday on
  (admin warning marks stay), so a rescheduled reminder fires again.
- `context.load_upcoming_service_events(force=True)` bypasses the 5-minute
  cache; «🔄 Сверить график» uses it.
- Informational notes render as `informational_label(task, values)` →
  «2я часть нет» (value from the neighbour cell); `_cell_date(value, year, month)` also
  accepts text `dd.mm` / `dd.mm.yy` (two-digit day and month) or `d.mm HH:MM`
  and month abbreviations, infers the year around the sheet month
  (December→January), never treats numbers as dates, and accepts a year-less
  `dd.mm` only for the sheet month ±1. `_process_service_reminder` re-checks
  the slot (`_same_reminder_slot`) before sending and before marking delivery.
- Preserve idempotent delivery records and per-service/event locking. A
  failed delivery must not be recorded as success.
- Personal reminders carry `build_personal_answer_kb` buttons
  (`pcf:{c|d}:{service}:{YYYYMMDD}:{task_token(task)}`, handler
  `on_personal_answer`; the token is a name hash because task positions get
  renumbered, resolved by `linked_task_by_token` among the caller's linked
  tasks). Answers live in `service_confirmations` (`set_confirmation`
  returns `(name, changed)`; only the contact linked to that task may
  answer). An answered task/date gets no more personal reminders. A new
  "declined" sends `notify_admins_declined` and calls the optional
  `context.on_assignment_declined` hook (replacement search, stage 3.3).
- Replacement (stage 3.3, `bot/replacement.py`): a new "declined" calls
  `context.on_assignment_declined` = `start_replacement_search` (notice +
  non-anonymous poll via `send_to_service_group`, row in `replacement_polls`,
  `context.schedule_replacement_close` adds APScheduler job
  `replacement-poll-{poll_id}`; open polls are rescheduled on startup).
  `poll_deadline`: min(now+12h, event day start−12h), at least 1h. Votes:
  `on_replacement_poll_answer` → `record_poll_answer`. `finish_replacement`
  closes once (`close_replacement_poll` returns False on repeat), picks the
  first "Могу" among `task_member_ids`, else the first "Могу". `/change task
  @username [dd.mm]` (`on_change_command`, admins only) uses an unfilled
  request if there is one, otherwise works without a decline (stage 3.4) on
  the given or nearest date where the task has exactly one person. The
  «🔁 Замена» card screen (`open_replace_dates`, callbacks `rpl:{session}:…`,
  `pending_inputs` operation `service_replace`, Telegram pick via
  `pending_shares` "service_replace") and `/change` both go through
  `admin_assign_replacement`, which closes an active request for that person;
  otherwise it calls `assign_replacement(..., expect_current=True)` →
  `set_replacement(expect_current=True)` (BEGIN IMMEDIATE + `is_replaced`
  chain check), so a stale screen returns False. `_notify` returns failed
  chats; failed announcements warn service admins and the actor.
  `context.load_upcoming_service_events(until=...)`: the cache holds events
  through the end of next month, default return is 6 weeks.
  `start_replacement_search` ignores a decline of an already replaced person
  and holds a per-(service, date, task, person) asyncio lock across check +
  Telegram sends + insert, so concurrent declines start one poll. If the
  close timer cannot be scheduled after the insert, poll and row stay open
  (startup reschedules open polls; `/change` works) and admins are told.
  `on_personal_answer` rejects a stale button when the schedule (with
  replacements) has the task on that date but not this Telegram user
  (`scheduled_name`: contact name among scheduled names, else a replacement
  row with this `telegram_id`; no data → accept). The scheduled name is stored
  in the confirmation and used for the replacement search.
  Every decline stores a request row in
  `replacement_polls` (no group chat or failed poll → `manual-…` row with
  status `closed`); `filled` marks a request that got a replacement
  (`claim_replacement_request` / `release_replacement_request`), and `/change`
  accepts any unfilled request from today (`list_unfilled_replacement_requests`).
  `service_replacements` override Excel for one date:
  `presentation.apply_replacements` runs in `_load_service_task_assignments`
  only with `only_date` (month-wide lists stay raw; roster/contact sync always
  sees raw names), in the admin report (reports store raw assignments via
  `raw_out` and apply current replacements on render) and, via
  `_apply_event_replacements`, on the cached upcoming events. `set_replacement` adds a task contact only for a
  substitute already known in the service (member or contact). Poll votes use
  `_connection(notify=False)`, so they do not trigger backups or job
  refreshes; `/change` removes the pending close job
  (`context.cancel_replacement_close`). Group sending lives in `bot/group_send.py`.
- Personal reminders (`user_reminder_schedules.origin`/`body`, migrated:
  rules of a task that had its own text became `user`): `admin` rules use the
  admin template and the user may only toggle or copy them; `user` rules have
  their own time/text (`update_schedule(body=...)`). `add_schedule` enforces
  `USER_REMINDERS_PER_SERVICE` (10 own `user` rules) per user and service and
  `_ensure_unique_enabled_rule` (no two enabled rules of a task at the same
  days/time, also in `update_schedule`); own text ≤ 3000 chars;
  `set_user_service_schedule` touches only `admin` rules. Screens
  (`render_current_personal_level`): `service` (rule list), `rule`,
  `delete_rule`, `add_task`, `add_time`. Sent text is always
  `🔔 {service} · {task} · {date}` + body (shortened with «…» if it
  exceeds the limit). Enabling/adding a single rule never flips the service
  preference. Migration `personal-origin-v2` (catalog_migrations): only a
  lone default (1, 18:00) rule without body stays `admin`; task bodies move
  into its rules or into a disabled `user` rule. «📤 Отправить сейчас» keeps a
  reminder snapshot and refuses to send if the reminder changed.
- Group reminder card: «📤 Отправить сейчас» (`send_now` → preview →
  `send_now_ok`, not marked delivered).
- `_send_dialog` prefixes buttons without a known icon with 📋; add new
  leading emoji to its list.
- `bot/admin_report.py`: after a group reminder `send_admin_reports` sends
  one report per service admin and date (fallback: super admin) and stores it
  in `admin_report_messages`; later reminders and answers edit it
  (`refresh_admin_reports` delegates to `send_admin_reports`, which targets
  only current recipients, drops rows of removed admins, and resends a report
  whose edit fails). `_process_service_reminder` calls it whenever the group
  reminder of that date is delivered, so a failed report is retried without
  resending the group. `build_report_text(limit=...)` keeps
  `{confirmation_status}` visible by collapsing delivery, then assignments;
  confirmations are keyed by `(task, telegram_id)`. The template is written on
  finish only if this session edited it (`admin_report_template_edited`). Template `services.admin_report_template`
  (`ADMIN_REPORT_FIELDS`), edited under «📋 Отчёт админам» in
  `/service_notify` and saved on «Завершить и сохранить».
- Template screens («📝 Шаблон по умолчанию», «📋 Отчёт админам») have
  «👁 Предпросмотр» (`group_reminder:tpl_preview`, step `template_preview`,
  back via `tpl_back`): `context.get_template_preview(service_id, kind,
  template)` → `app.get_template_preview` renders the session draft
  (`pending["group_template"]` / `pending["admin_report_template"]`, None =
  built-in) for the next Sunday; the report uses raw assignments
  (`raw_out`) because `admin_report.report_text` applies replacements itself.
  Field lists in input prompts come from `template_fields_help(...)`
  (`TEMPLATE_FIELD_HELP`), one field per line with a short explanation.

### Stage 5: readiness poll and schedule planning

- `bot/schedule_planner.py` is pure (no Telegram/DB): `PlanInput` (dates,
  `needed` per task, `free` dates per person, `tasks` per person = «Списки»
  ∪ bot bindings, `last_served` from the previous month) → `build_plan` →
  `Plan.slots[(date, task)]`. Per date a min-cost maximum matching
  (successive shortest paths) guarantees maximum coverage; cost = marginal
  of `LOAD_WEIGHT × load²` + `GAP_PENALTY` (1 week 6, 2 weeks 2); then
  `improve_plan` does replace/swap moves (swaps only across dates) that
  keep one task per person per day; each move is scored by the change of
  the affected people's `_person_cost` only, and only the best move is
  applied. Keep it deterministic (sorted iteration). A dense month (5 dates
  × 30 seats, 30 people) takes ~0.1 s on the Pi; still call it via
  `asyncio.to_thread` from handlers/jobs.
- `bot/readiness.py` (step 2): one Telegram poll per service and month
  (Sundays of the next month + `CANT_OPTION`, multiple answers). Tables
  `readiness_settings` (enabled off by default, start/deadline time,
  duration 1–6, remind 0–5 < duration), `readiness_runs` (UNIQUE service +
  month; open/closed/skipped; poll/chat/message ids; UTC deadline),
  `readiness_answers` (dates JSON; [] = «Не смогу»; retract deletes),
  `readiness_notices` (one-off preview/reminder/launch_failed claims).
  `run_readiness_tick` (job `readiness`, every minute): preview a day before
  `launch_date` (last Sunday − 6 days), auto-launch, personal reminder,
  close at deadline (stop_poll + admins get `progress_text`). Launch/close
  per service run under `service_lock`. People = `readiness_people`
  (members ∪ responsible_contacts, enabled tasks only). Poll answers are
  routed in `on_replacement_poll_answer`: a readiness poll_id goes to
  `record_readiness_answer` via `answer_dates`. UI: card → «🗳 Опрос
  готовности» (`rdy:<action>:<service>[:arg]`, service admins), stateless
  callbacks checked by `is_service_admin`. Lifecycle callbacks (start, skip,
  unskip, progress, extend, finish) carry the month `YYYYMM` and are
  rejected as stale after the month changes; skip/unskip/extend run under
  `service_lock`; extend refuses an expired deadline.
- Reliability: auto-launch claims `launch` once (no per-minute retries);
  "missed" alert only if `enabled_at` ≤ scheduled start and no `launch`
  claim, checked for the current and target months. Closing sets
  `poll_stopped` / `result_sent` after success; `list_unfinished_readiness_runs`
  retries them each tick for up to a day. Reminders are marked per person in
  `readiness_reminders` after delivery (Forbidden/BadRequest count as done).
  `readiness_people` keys people by Telegram ID (`key` is unique even for
  equal names; planner uses `key`).
- Readiness texts: `intro_template` / `reminder_template` in
  `readiness_settings` (NULL = `DEFAULT_READINESS_INTRO` /
  `DEFAULT_READINESS_REMINDER`, fields `READINESS_INTRO_FIELDS` /
  `READINESS_REMINDER_FIELDS`), rendered by `readiness.render_template`
  (drops an empty «👥» line). UI `rdy:tpl|tpledit|tplreset:<service>:intro|rem`;
  text input is pending operation `readiness_text` in `on_service_parameter`.
- Navigation trace: level `TRACE` (5) in `bot/logger.py`, enabled by
  `--trace` / `manage.sh trace on` (flag `.runtime/trace_enabled`).
  `admin_router` outer middlewares `trace_callback` / `trace_message` and
  `_send_dialog` / `on_nav_button` log with `debug.log(TRACE, ...)` — never
  free text of user messages (commands — only the command word). Trace
  middlewares sit on the dispatcher observers (all routers), falling back to
  `admin_router` when the dispatcher is a test stub. `back_callback` falls
  back to the last «✖️ Закрыть настройку» / «✖️ Отмена» (`_CLOSE_TEXTS`)
  when a screen has no «⬅️ …»; the root «✖️ Закрыть» of /my_notifications is
  not a back button. Nav hint ids are persisted in `nav_hints`;
  `drop_old_hints` removes an id only after the message is deleted (or is
  already gone), also on root screens when the keyboard was already
  replaced. A user message marks `hint_stale`; the next screen with a back
  button re-sends the hint above itself (`show_nav_keyboard(force=True)`).
- Readiness restart: `rdy:restart` (confirmation) → `rdy:restartok` under
  `service_lock`: stop the open poll (abort if that fails), park the old run
  under month `KEY#id` (`move_readiness_run`), `launch_run` with
  `manual_deadline`; on success `delete_readiness_run` (answers and reminder
  marks too, outside the rollback `try`); on failure move it back as closed.
  The old poll is stopped whenever `poll_stopped` is false (abort if that
  fails). Parked runs (`month` with `#`) are excluded from open/unfinished
  lists and settled each tick by `settle_parked_run` (new run exists →
  delete, else restore closed).
- Telegram delivery: `readiness.unreachable` (Forbidden, or BadRequest with
  «chat not found» / «user is deactivated» / «bot was blocked») = final, no
  retry; other errors retry. The result notice is marked per recipient
  (`readiness_notices` kind `result-<run>:<admin>`), so retries go only to
  admins who did not get it. Nav hint deletes: any BadRequest/Forbidden is
  final; at most 10 failed ids are kept.
- When the poll is stopped, `announce_closed` replies to the poll in the
  service chat once. «➕ В состав»: `rdy:addm:<service>:<telegram_id>` →
  pending `readiness_member` (selected tasks, not cleared by `addmt`/`addms`)
  → `bind_service_member(..., from_readiness=True)` (service editor allowed;
  refuses a name bound to another Telegram id) → progress re-shown. The name
  is the roster name when `readiness_people` already has that Telegram id,
  else the Telegram name; the task list is frozen in pending (`tasks`).
- Third readiness text: `closed_template` (`DEFAULT_READINESS_CLOSED`,
  fields `READINESS_CLOSED_FIELDS`), kind `done` in `READINESS_TEXTS`,
  rendered by `readiness.closed_text` in `announce_closed`.
- Publication titles: `plan_title_template` / `plan_update_template`
  (`DEFAULT_PLAN_TITLE` / `DEFAULT_PLAN_UPDATE_TITLE`, fields
  service_name, month), kinds `pub` / `pubupd`, rendered by
  `schedule_plan.publication_title(service, month, update, settings)`.
  The old `{status}` field was split by the one-off migration
  `plan-title-v2`. Short phrases: `READINESS_PHRASES` (name → default,
  fields, limit, label), stored in `readiness_settings.phrases_json`
  (`settings["phrases"]`), set via `set_readiness_phrase`, read via
  `readiness.phrase(settings, name)`; UI kinds `ph.<name>` under
  «🔤 Фразы» (`rdy:phr`), no preview. `publication_text` puts each date
  on its own line with tasks below it (two-space indent), blank line
  between dates.
- Reminder sends treat any Forbidden/BadRequest as final (marked reminded). Preview:
  `rdy:tplprev:<service>:intro|rem`. `launch_run` records all intro chunk
  ids and deletes them all if the poll fails.
- `service_tasks.people_needed` (minimum 1–5, default 1) and `people_max`
  (NULL = minimum, up to 6) — «👥 Нужно людей», set by service admins too
  (`set_service_task_people_needed(actor, service, task, low, high)`) from
  «🗳 Опрос готовности» or «📋 Задачи служения» (`rdy:people|ppick|pset`).
- One plan in the bot (decision 15): `import_excel_plan` (source
  'excel', approved, unpublished) from «📊 График Excel»
  (`service:service_excel`, `xls:load|ok|no`; `schedule_plan.excel_plan`
  builds slots/days from Excel events via `week_events`). Approved bot
  plans override Excel for reminders/replacements/overviews:
  `app._apply_bot_plans` (in `load_upcoming_service_events` window,
  `excel_only=True` skips it) and the per-date overlay in
  `_load_service_task_assignments`; `schedule_plan.plan_events` turns an
  approved plan into ServiceEvents (own-day tasks on their date).
  `plan_current` treats an Excel plan as current.
- Week bundles: `service_tasks.same_week` (`set_service_task_same_week`,
  UI `svc_tasks:week|wtog`) and `service_members.week_tasks_json`
  (`set_service_member_week_tasks`, None = as service, [] = never,
  ≥2 tasks; card `mbr:wk|wt|ws|wa|wn`). `readiness.summarize` builds
  `PlanInput.bundles`; `schedule_planner.build_plan` wraps `_build_plan`
  with a virtual seat `task\x1ftask` per bundle group (non-bundled people
  may take it for one task; `_fill_bundle_gaps` fills the rest), and
  falls back to the plain plan if coverage drops. `Plan.load` and
  `served_dates` count dates, not slots.
- PLAN decision 16 (planner): equal-cost ties go by `_priority` — fewer
  picked dates, longer unserved (`last_served`), earlier answer
  (`PlanInput.answer_order`, from `summary.answered` insertion order).
  `_build_plan.assign` scales costs so lower digits are: bundle seat fit
  (`seat_cost` on the person→seat edge of `_min_cost_matching`: a virtual
  seat costs more for someone without that bundle), then priority. A
  non-bundled person on a virtual seat takes the task that leaves the
  other bundle seats fillable (`unfillable`, spare within max).
  `build_plan` = `_bundled_plan` + `_place_losers`: a respondent with
  picked dates but no service replaces a non-respondent on another date
  (largest cost drop, not back-to-back with `last_served`, once; a
  non-respondent's bundle moves only to someone with that bundle).
  `plan_text` marks a respondent on an unpicked date «°» (poll plans only).
- Free-days chat notice: `readiness.send_free_days_reminder` (called every
  poll tick while open; notice kind `free-days-<run id>`, once) when
  answered × 3 ≥ voters × 2 (voters = answered + silent with Telegram)
  and `free_days` (→ `schedule_planner.open_dates`: per-date matching of
  respondents only, bundles as extra seats) is non-empty. Text: phrases
  `free_days` ({days}, poll option labels) + `free_days_vote` when silent
  voters exist.
- `service_tasks.day_offset` — task's own day (−6…+6 from the plan's Sunday,
  NULL = service days), `set_service_task_day` (also rewrites `tasks_json`
  of current/future plans and marks approved ones `republish`); UI
  «📅 Дни задач» `svc_tasks:days|dpick|dset`. Plan tasks carry `day`;
  `schedule_plan.slot_offset` (override → task day → personal day),
  `task_days`, `week_groups` / `week_person_day`: any task day switches
  `plan_text` and `publication_text` to weekly layout. `served_offsets`
  keeps the latest day per person per date. Excel has own-day tasks as
  separate dates: `excel_rows` writes them as blocks of their own date,
  `week_events` folds such Excel events into their Sunday for the
  «👥 Ответственные» overview (`days` → «· ср 11.11»).
- Planner: `PlanInput.extra` (optional seats above the minimum) and
  `reluctant` (non-respondents: free on all dates, `RELUCTANT_WEIGHT` per
  service). A one-week gap costs `BACK_TO_BACK_WEIGHT` (100 ×
  `RELUCTANT_WEIGHT`): back-to-back only when a required seat would stay
  empty. Two passes over the month: required seats for all dates (then
  `improve_plan`), then optional seats only for respondents with no
  service within a week either way; improvement never puts a reluctant
  person beyond `needed`. `summarize` adds non-respondents (incl. no
  Telegram) as reluctant; `progress_text` lists respondents with dates and
  the plan per Sunday and task («*» = non-respondent).
- Monthly limit (step 4a): `PlanInput.limits` {key: (min, max|None)} from
  `summarize` (personal `service_members.month_min/max` via
  `readiness_people` `month_limit`, else `readiness_settings.month_min/
  max`). Max is soft (`ABOVE_MAX_WEIGHT`): required seats may exceed it when
  nobody else can fill them, optional seats never do; `improve_plan` replace
  costs it through `_person_cost`; min is
  `BELOW_MIN_WEIGHT` per missing service in `_person_cost` (cheaper in
  pass-1 costs). After pass 1 `repair_gaps` augments the plan as a max
  flow (source → person [max] → (person, date) [1] → seat [needed] →
  sink), so required gaps caused by caps are closed whenever possible;
  kept people stay in place. `build_plan` then runs `_spread_soft_limits`
  (direct replacement by someone under the max, otherwise
  `_rearrange_over_limit`: a BFS chain where a person at the max takes
  the seat and gives up another, until someone under the max; applied only
  if total excess drops and nobody gets two tasks on one service day).
  UI: `rdy:lim` (section) and `mbr:lim|lset` (card);
  values `n`, `a_b`, `a_x`, `m` (pick min), `m<a>` (pick max), `s` (as
  service).
- Removing a member (in the lists or not) records
  `service_member_exclusions`;
  `sync_service_rosters` skips those names until the person is added in
  the bot again (`_clear_member_exclusion`).
- Binding by @username only (`parse_manual_user(username_only=True)` in
  service_contact): a username the bot has seen (`known_telegram_id`)
  gets the usual ID confirmation; otherwise `ubind:ok|no` saves
  `username_bindings` (service, name, task '' = all tasks). The outer
  middleware `username_binding_middleware` (message, callback_query,
  poll_answer) completes it on the user's next update
  (`take_username_bindings` → set_responsible_contact /
  bind_service_member as the admin who set it) and messages that admin.
  Binding by ID or removal drops the pending row. Pending rows expire
  after `USERNAME_BINDING_DAYS` (30); completion is refused (admin gets
  ❌) when the user is already bound to another name in the service;
  `known_telegram_id` uses only observations: `seen_usernames` (written
  by the middleware when a user's username changes, `note_seen_username`
  forgets the ID's previous username; no username is stored as the marker
  `~<id>`, which retires that ID's older poll answers and is never looked
  up) and `readiness_answers`, newest
  first; admin-entered usernames don't count. A new pending binding for a
  person replaces older ones (any task); `set_service_member_tasks` drops
  pending rows of removed tasks; the card shows a pending username next
  to the current account.
- Launch reserves the run row before sending (`attach_readiness_poll`
  after), deletes it on failure. Closing notice is tracked by
  `closed_announced` and retried; the admin result is fitted into one
  message (`fit_one_screen`). `settle_parked_run` re-reads under the lock,
  restores over a skipped marker, keeps the result decision; skip is
  refused while a run is parked. «➕ В состав» buttons carry a pending
  token; `bind_service_member(from_readiness=True)` refuses names bound to
  another Telegram in `service_members` or `responsible_contacts`.
- Launch failure modes: intro chunks fall back to the general topic one by
  one (`send_chunk` → `send_to_service_group`); a network/timeout error on
  `send_poll` keeps the month closed (no auto relaunch) and asks admins to
  check the chat; an unattached poll that cannot be stopped keeps a closed
  run with its ids so the tick retries `stop_poll`. Votes that arrive
  before the poll id is attached are held (`hold_early_answer`, 2 min) and
  replayed after attach. Preview/missed notices are released if delivery
  failed. A failed restart restores the old run without suppressing its
  result. `_send_dialog` keeps the stored back route for messages without
  inline buttons. «➕ В состав» buttons carry `YYYYMM.<telegram_id>`.
- Step 3 (`bot/schedule_plan.py`): `schedule_plans` (PK service + month;
  `slots` working copy date → task → person keys, `approved` published copy,
  `dirty` = they differ; `run_id`, `tasks` with needed/max, `last_served`,
  `note`, `edited`, `source` poll|excel for step 4). `finish_run` calls
  `ensure_plan` (rebuilds when `run_id` differs, e.g. after a restart) and
  sends `plan_text` with `rdy:plan`. `last_served` = previous month's approved
  plan, else Excel via `events_loader` (= `context.load_upcoming_service_events`,
  threaded through `run_readiness_tick` → `close_run` → `finish_run`); unique
  normalized names only. UI actions `PLAN_ACTIONS` in `on_plan_action`:
  `plan`, `precalc[ok]`, `pedit` → `pdate` (`YYYYMM.d`) → `ptask`
  (`YYYYMM.d.t`) → pending `readiness_plan` with token + `keys`, `padd` /
  `prem` / `pclear` (`token.i`); `pok` → `pokok` → `publish` (chat, then
  `approve_schedule_plan`) and `send_excel_block` (text + `excel_file`
  .xlsx). Plan months allowed: target month or current month
  (`plan_month`). Edits run under `service_lock` on a fresh read; one task
  per day enforced (`busy_on`); `max == 1` replaces.
- Feedback 24.10: plan/progress list one person per line (`readiness.task_lines`),
  load per line `N — name` sorted desc (admins only). Reminder day also posts a
  general chat reminder (`send_chat_reminder`, reply to poll, notice kind
  `chat-reminder-<run>`, template `chat_reminder_template` / «📣 Напоминание в
  чат»); «📊 Ход опроса» shows `reminder_line`. Roster («👥 Состав»): person →
  member card (`show_member_card`, callbacks `mbr:<action>:<service>:<token>[.i]`,
  state `pending["member_card"]` in the `service_contact` session) with
  «🔗 Telegram» (→ `offer_contact_user_selection`), «📝 Задачи»
  (`set_service_member_tasks`: `service_members.tasks_manual = 1`, roster sync
  keeps those tasks and skips their auto-contacts; contacts limited to chosen
  tasks) and «🗑 Удалить» (`remove_service_member`; a «Списки» person returns on
  the next sync — warned). Bot-added members (source manual, not in list) are
  marked 🆕.
- Section renamed «📅 График» (still `rdy:` callbacks). No-poll plan: `pfree`
  → `schedule_plan.build_draft(free_run(...))` with `summarize(assume_free=True)`
  (everyone with tasks free on all Sundays, not reluctant); stored with
  `run_id = NULL`; `plan_current` treats it as current while the month has no
  run or a skipped one; `plan_run`/`plan_summary` give screens the right
  summary. Slot editor `pnew` → reply keyboard `request_users`
  (`pending_shares` kind `plan_user`) → `on_plan_user_shared`: existing person
  by Telegram id, else `bind_service_member(from_readiness=True, tasks=[task])`,
  then `change_slot(..., "padd", key)`; `⬅️ Назад` text returns via
  `reshow_plan_slot`. `_send_dialog`: a screen sent in reply to a `/command`
  deletes the previous menu screen (`nav_state[actor]["menu"]`), recording the
  new screen before awaiting the delete.
- Task-less services (catalog `tasks: []`): `ServiceRegistry.plan_tasks` returns
  one implicit task named after the service (position "s", people_needed /
  people_max in readiness_settings); `readiness_people` gives everyone that task;
  `summarize` / `schedule_plan.plan_tasks` / «👥 Нужно людей» use `plan_tasks`.
- Planner rotation: `_rotation_penalty` = max(0, ROTATION_WEEKS − weeks from
  last_served to the month's first date), added once per person (first
  service of the month) in `assign` cost and `_person_cost`; smaller than a
  load step so load balance wins. Day window `readiness_settings.day_from/day_to`
  («📆 Дни служения», `DAY_WINDOWS`), personal day `service_members.day_offset`
  («📅 Свой день», `set_service_member_day`, `mbr:day|dset`); shown via
  `schedule_plan.day_label` / `person_day` in `plan_text` and
  `publication_text` (`window_of`). Reminders by window/personal day — step 4.
- Task-less plans (`schedule_plan.implicit_plan`, tasks carry `implicit`): text,
  publication and Excel text show people directly under the date
  (`task_lines(bare=True)`, `Summary.implicit`); `pdate` jumps to the slot.
  Per-date day: `schedule_plans.days_json` / `approved_days_json`,
  `set_schedule_day`, UI `pday`/`pdset` (token.i[.offset|n|back]);
  `schedule_plan.day_offset` = per-date > personal; `set_schedule_slots` drops
  overrides of people no longer on that date; revision includes days.
- «📅 Свой день» also from the «📅 График» section: `show_own_days`
  (pending `readiness_own` with token + member names; `rdy:own` → `ownp`
  token.i → `owns` token.i.offset|n). `schedule_plan.date_window` drops the
  window from a date header when everyone on that date has their own day.
- Permissions (24.10): a service admin may do everything for own services
  (chat, contacts/bindings, members, tasks, sync, notify, replace, «📅 График»);
  only `ADMIN_OPERATIONS` (service_admin_add/remove) stay super-admin-only;
  registry uses `_require_service_editor` everywhere else; `/test_notify`
  command — main admin only, but the card's «🧪 Проверка доставки»
  (`can_check_delivery`: service admin + group chat set, also offered right
  after a chat is saved) is open to the service admin; previews go to the
  caller. Start menu (27.10): settings, notify, contacts, and for the super
  admin `menu:service_admins` (submenu with add/remove); sync, chat and
  delivery check live only in the service card. Card (27.10 evening):
  chat and admin add/remove moved into «⚙️ Настройки»
  (`service:service_setup`, `service_setup_keyboard`); the delivery check
  is in the chat screen (`service_chat_keyboard(service_id, actor_id)`);
  «❓ Помощь» = `service:service_help` (`SERVICE_HELP`). `/service_contact`
  opens the member list first (`offer_contact_overview(members_first=True)`),
  the date overview via «🗓 По датам графика».
- Readiness poll texts: `question_template` (`readiness.question_text`, fields
  service_name/month/month_in, ≤300, sent with `question_parse_mode=None`,
  blank → default); «📝 Тексты» (`show_readiness_texts`, kind "q").
  Poll options by day window (`readiness.option_label`), intro footnote
  `own_days_note` (readiness_people `in_roster`, @username for same names).
- Reverse mode («↔️ Наоборот», `readiness_settings.reverse_poll`, snapshotted
  in `readiness_runs.reverse_poll`): marked options mean «cannot»;
  `question_text(..., reverse_poll)` and `intro_text(..., reverse_poll)` use
  `DEFAULT_READINESS_QUESTION_REVERSE` / `DEFAULT_READINESS_INTRO_REVERSE`
  when no custom template is set; a custom question gets a «НЕ сможешь»
  suffix (kept inside the 300 limit), a custom intro an explanatory
  paragraph. Pass `settings["reverse_poll"]` everywhere these are rendered
  (`launch_run` and the `rdy:tplprev` previews `intro`/`q`). The personal
  `DEFAULT_READINESS_REMINDER` is deliberately mode-neutral («не ответил(а)
  на опрос по служению на {month}»); keep new default texts neutral or add
  a reverse variant.
- Linked-task fallback (`schedule_planner._can_serve`): a bundled person who
  only marked the later task may serve the earlier one unless
  `_alternate_covers` says another respondent really covers the seat. It
  compares the max matchings of the date's required seats without and with
  the fallback, so an alternate needed for another seat the same day does
  not block it. Do not reintroduce a plain «any other candidate exists»
  check.
- Approved plan snapshot `served_offsets_json` (`schedule_plan.served_offsets`,
  passed to `approve_schedule_plan`); `last_served` uses it (legacy plans:
  approved_days, else personal day). Planner: gap under a week counts as
  1 week; rotation weeks clamped ≥0.
- Chat binding by service admins: `set_group_chat` — group only (ID<0), not
  bound to another service (own chat OK); handlers `require_chat_member`
  (get_chat type group/supergroup + get_chat_member; errors/timeouts →
  PermissionError) for manual IDs. Interactive loader passes
  `own_roster_only=True` (sync only that service's «Списки»).
- User guides: `docs/ADMIN_GUIDE.md` and `docs/VOLUNTEER_GUIDE.md` (Russian,
  schematic screens). Update them together with UI changes.
- Customer decision 5 (24.10): Excel may stop being filled — step 4 adds a
  per-service source switch and «📥 Загрузить график из Excel».

### Rich text, mentions, and message length

- Never build HTML by hand in message text. Use markers: `code(value)` for
  code spans and `user_link(telegram_id, label)` for mentions; the renderer
  (`render_screen_chunks` / `send_telegram_screen`) escapes everything else.
- Text sent with `parse_mode=None` must go through `plain_text()` first.
- Measure Telegram limits with `visible_units()` (decoded text), not the raw
  marker string. Markers are never split across chunks; a link label longer
  than the screen is shown as plain text. Malformed markers render as
  literal text instead of raising. Templates reject `\x00`.
- Linked contacts render as `Имя (@username)` wrapped in a mention.

### Roles, /start, and command menus

- Roles: super admin (registry `super_admins`), service admin, participant;
  the configured main admin (`ADMIN_TG_ID`) additionally gets `/test_notify`
  even if the registry resolves them as a participant.
- `/start` shows inline buttons (`menu:<operation>`) for the caller's role;
  unlinked users see their Telegram ID. Command menus are set per chat via
  `BotCommandScopeChat` at startup, on `/start`, and on admin add/remove;
  "chat not found" for users who never opened the bot is logged at info.

### Responsibles overview and personal schedule

- `/service_contact` and `/service_sync` open an overview of upcoming events
  (6 weeks, via `context.load_upcoming_service_events`) with per-person link
  status; without that loader they fall back to the task list. Do not show
  Telegram IDs on these screens.
- `_load_upcoming_service_events` skips only a missing future sheet
  (`SheetNotFoundError`); malformed sheets propagate. The loaded events are
  cached for 5 minutes per process behind a lock.
- `/my_schedule` lists the user's own non-informational assignments.
- Personal service-level time uses `set_user_service_schedule`: it updates
  the first rule in place, deletes extras after copying their delivery
  records, and creates rules for tasks without one. Enabling a service with
  no rules installs `DEFAULT_PERSONAL_RULE`. `ensure_default_personal_rule`
  returns `None` on success or the reason text. Success requires an enabled
  rule on a task the user has in the current schedule; rules of tasks absent
  from the schedule do not count. Every current task without any rule gets
  the default rule, even when other tasks already have rules; disabled rules
  are never re-enabled automatically. Reasons:
  `PERSONAL_SCHEDULE_UNAVAILABLE` (schedule failed to load — stored rules
  alone are never trusted), `PERSONAL_NO_TASKS`, `PERSONAL_RULES_DISABLED`
  (every current task has rules and all are disabled). Bulk enable
  uses the short forms in `PERSONAL_PROBLEM_SHORT` because callback toasts
  are cut at 200 characters. Never mark a service enabled without a usable
  rule.
- `current_personal_tasks` caches only the Excel assignments; it re-reads
  bindings from SQLite on every call on purpose so admin changes apply
  immediately.
- Build contact-navigation callback data with
  `contact_navigation_callback`; use `contact_navigation_button` only when
  the labeled button itself is needed.
- Personal screens paginate only item rows; actions, page buttons, and
  back/close stay as separate bottom rows (`_dialog_keep_rows`).
- Stage 4.1 month summary (`bot/month_summary.py`): `user_month_summaries`
  (default off, 1 day / 18:00) and `month_summary_deliveries` (one per
  user+month, claimed before sending, released on failure). The
  `month-summary` cron job runs every minute; `due_month` is the window from
  `first Sunday − days` at the time until the end of the first Sunday. A
  summary waits until every linked service has events in that month; at the
  first Sunday at the user's time it goes with what exists (`SummaryRun.waiting`
  → the tick keeps running every minute but rereads the schedule at most every
  30 min; a loader failure pauses the tick for 10 min). A slot taken by a
  reminder with a different period offers only «➕ Отдельно» (no merge). No own tasks → marked, no
  message; Forbidden/BadRequest → mark kept. `load_upcoming_service_events(
  since=...)`: the cache holds events from the 1st of the current month; the
  default window starts today, «📆 На месяц» asks for the whole month. `personal_schedule_lines` (presentation) is shared by
  `/my_schedule` and the month view; «📆 На месяц» (`menu:my_month`,
  `mymonth:YYYYMM` for current/next month only).
- Stage 4.2 period: `group_reminders.active_from/active_until` (Sundays,
  NULL = open). `reminder_occurrence`/`next_reminder_occurrence(...,
  active_from, active_until)` return None after the period;
  `_process_service_reminder` skips events outside it;
  `disable_expired_group_reminders` runs on every reminder refresh and in the
  `reminder-period-expiry` job (00:05); an expired reminder can't be toggled
  on until a new period is set. Same-slot duplicates (`_ensure_group_schedule_unique`,
  `enabled_group_reminder_at`) count only when periods overlap
  (`periods_overlap`); merging reminders with different periods is refused.
  Period buttons encode `reminder_id * 100 + index` and `period_menu`
  checks the card's reminder id; the send-now snapshot includes the period,
  and send-now on an ended period answers without loading the preview. The
  month-summary waiting cache stores raw events
  (`load_upcoming_service_events(with_replacements=False)`) and applies
  replacements on every read; at the last chance it reads fresh (force, at
  most every 10 min). Job ids must not start with
  `group-reminder-` / `service-reminder-` (those prefixes are pruned).

### Service-selection menus and Telegram UI

- All service-selection commands (`/services`, `/service_settings`,
  `/service_contact`, `/service_notify`, `/service_chat`) use the shared
  service ordering and button factory where appropriate.
- Up to eight service buttons are shown without pagination in the standard
  compact layout (normally 3+3+2). Larger lists paginate within Telegram's
  button limit. Page navigation belongs in a separate bottom row; include
  navigation and pinned actions when calculating page capacity.
- `layout_buttons` should balance rows while respecting label width and the
  three-button-per-row limit. Try alternate contiguous boundaries at the same
  row count before adding a row. Keep long/single-action buttons on their own
  row and navigation at the bottom.
- Validate callback data (maximum 64 UTF-8 bytes), keyboard row/count limits,
  and Telegram text limits (UTF-16 code units). Preserve callback data and
  ordering when changing layouts.
- Conversation handlers must reject stale sessions and enforce service/admin
  permissions before mutating state. Keep callback toasts short and plain.
- Bottom reply keyboard «↩️ Отмена» / «🏠 В начало» (`bot/navigation.py`):
  `_send_dialog` shows it (with a `NAV_HINT` message) for private screens
  whose inline markup has a back button (`back_callback`: last «⬅️/↩️»
  button, paging `ui_page:` excluded) and hides it on screens without one
  (send `ReplyKeyboardRemove`, then delete it and the hint). Per-actor
  `nav_state` keeps `shown`, `helpers`, `back`, `screen`. Any other reply
  keyboard (contact/chat pickers, `ReplyKeyboardRemove`) resets `shown`
  — via `_send_dialog` and the bot session middleware
  `track_reply_keyboards` (`replaces_nav_keyboard`). `on_nav_button` is
  registered before the generic text handler: «Отмена» replays the stored
  back `callback_data` as a synthetic `CallbackQuery` (`id=NAV_CALLBACK_ID`,
  message = `ScreenMessage` of the last screen) through
  `admin_router`/`router.propagate_event`; `_answer_callback` sends only
  ⚠️/❌ answers for it (`ScreenMessage.notify`). No back button, «В начало»,
  or a replay that sent no screen (`ScreenMessage.sent == 0`) →
  `show_start`. Back buttons are recognised by `_BACK_PREFIXES` («⬅️ К »,
  «↩️ К », «⬅️ Назад», «⬅️ Отмена»); paging («⬅️ Стр. N», `ui_page:`,
  `task_page:`, «⬅️ Назад» on `contact_nav:`,
  «⬅️ Предыдущее», dates) are not. `back` changes only in `_send_dialog`
  (a background reminder must not break «Отмена»); it is restored if the
  replay raises before a new screen. Show/hide run under a per-actor
  `nav_locks` lock; a failed `ReplyKeyboardRemove` keeps `shown=True` so the
  next root screen retries. `/start` seeds unknown users as shown and calls
  `hide_nav_keyboard` itself (its menu may have no inline buttons). New
  screens must keep a «⬅️ К …» back button for this to work.

### SQLite, backups, and restore

- Use `ServiceRegistry` methods for persistent state; avoid ad hoc SQL except
  in migrations or focused tests. Migrations must be safe to rerun and
  preserve legacy data/IDs where compatibility requires it.
- `executescript` commits any pending transaction before it runs. A
  migration script that rebuilds a table (`CREATE … _v2` / `DROP` /
  `RENAME`) must start with `BEGIN IMMEDIATE;` so it stays atomic; the
  enclosing `with connection` commits it.
- Snapshots are environment-specific and must be fully written, validated
  (manifest/checksums and SQLite integrity), then atomically published.
  Credentials, tokens, logs, and temporary files do not belong in a snapshot.
- Current retention is bounded to 14 validated snapshots per environment and
  retains at least 7 recovery snapshots. Prune only older validated snapshots
  after a new snapshot has been committed; do not delete invalid/unvalidated
  recovery data as if it were safely disposable.
- Restore validates all components and environment before replacing targets,
  uses staging beside targets, and refuses unsafe/running-bot conditions.
  Never auto-delete SQLite `-wal`/`-shm` files to force a restore.

### Web App and deployment

- `webapp/` is static content served from its own root. Do not expose project,
  configuration, database, or runtime files. Keep it bound behind the
  configured HTTPS reverse proxy; the static server is not an authenticated
  application API.
- Never put tokens or private data in browser assets, URLs, diagnostics, or
  logs. Do not treat client-side `initDataUnsafe` as authentication.
- `manage.sh` controls bot, Web App, logs, backups, and restore in test/prod
  deployments. Test and prod use separate directories/configuration. Avoid
  deployment commands during ordinary code changes.
- Startup must not block on Telegram before the PID file is written: `manage.sh start` kills the process if the PID file is missing after 30 s. After a reboot DNS may be down for minutes and Telegram may answer 502. Command menus (`sync_admin_commands`, returns False on retryable errors) run as a background task after `_write_pid` and retry with backoff; `_wait_for_telegram` retries `getMe` before `start_polling` (aiogram's own `getMe` there has no retry). Menu writes for one user are serialized by a per-user lock in `bot/commands.py` (role read + request). Cron has an hourly `start` watchdog.

## Focused test modules

- Excel parser and informational-task behavior: `tests/test_excel_parser.py`
- Schedule and reminder occurrence rules: `tests/test_scheduler.py`,
  `tests/test_service_preview.py`
- Telegram workflows, sessions, permissions, service menus, `/start`,
  responsibles overview, personal screens: `tests/test_handlers.py`
- Command menus by role: `tests/test_commands.py`
- Assignment formatting and mentions: `tests/test_presentation.py`
- Button layout, pagination, screen rendering: `tests/test_ui.py`
- Registry and migrations: `tests/test_service_registry.py`,
  `tests/test_config_service_registry.py`
- Snapshot creation, retention, and restore: `tests/test_backup.py`
- Web App static server/config: `tests/test_webapp_server.py`,
  `tests/test_config_service_registry.py`
- Answers to personal reminders and admin reports: `tests/test_admin_report.py`
- Replacement polls, `/change`, Excel override: `tests/test_replacement.py`
- Runtime/CLI setup and dry-run: `tests/test_runtime_entrypoint.py`,
  `tests/test_debug_schedule.py`

## Relevant project documentation

- `README.md` — setup, commands, operations, and development workflow.
- `TZ1.txt`, `TZ2.txt` — detailed behavioral, UI, data, backup, and Web App
  requirements.
- `UI_SKETCHES.md` — representative Telegram screen layouts (v1, current);
  `UI_SKETCHES_V2.md` — menu redesign proposal (pending approval).
- `PLAN.md`, `EXECUTION_PLAN.md`, `BACKLOG.md` — customer decisions, status,
  technical tasks.
