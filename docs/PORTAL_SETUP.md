# PORTAL_SETUP.md -- Discord Developer Portal and client setup

Everything outside this repo that must be configured before Marginalia works.
Do it once, in order. Steps 1-9 are portal, 10-15 are the Discord client.

**Nearly every failure here is silent** -- no exception, no log line, a command
that returns nothing or a role ping that renders blue and notifies nobody. That
is why every step ends in BREAKS IF SKIPPED. Read those even if you skim.

Portal: <https://discord.com/developers/applications>

| Field | Value |
|---|---|
| Application name | Marginalia |
| Application ID | `1545535072151670824` (public) |
| Public Key | unused by this bot -- see step 5 |
| Bot token | never issued to the laptop; goes only in `.env` on the unRAID box |

Labels drift; the *goal* of each step is stated so you can match on intent.

---

## Step 0 -- Use a THROWAWAY guild first

Client -> `+` at the bottom of the server list -> Create My Own -> For me and my
friends -> `marginalia-scratch`. Add one text channel named `florilegium` and
two junk roles (`Marginalia 2026-09`, `Marginalia 2026-10`).

Run every step here first, then repeat steps 9-15 against the real club.

**BREAKS IF SKIPPED:** the three mistakes that cost the most surface for free in
a scratch guild -- role hierarchy (step 13), event-creation permission (step 8,
UNVERIFIED), and the private-thread default. And repeated 403s count toward
Discord's 10,000-invalid-requests-per-10-minutes ceiling, which ends in a
temporary Cloudflare IP ban on your whole host.

---

## Step 1 -- Confirm the application

General Information -> **Application ID** must read `1545535072151670824`.
Compare character by character.

**BREAKS IF SKIPPED:** with two test apps, every later toggle lands on the wrong
one. The symptom is maddening: the portal shows Server Members Intent enabled
and the bot still reports zero members, because you enabled it on app B while
the invite URL in step 8 hardcodes app A.

---

## Step 2 -- Name, description, icon

General Information: leave the name `Marginalia`, write a Description, upload a
square PNG >= 512x512, then **Save Changes**. On the **Bot** tab set the
Username and upload the avatar -- the app icon and the bot avatar are two
separate uploads and setting one does not set the other.

Suggested description:

> Reading-club companion for this server. Runs the monthly cycle, the nomination
> ballot, the reading schedule and checkpoint threads. Answers questions about
> the current book without spoiling past your progress. Never posts book files
> and can never ping @everyone.

**BREAKS IF SKIPPED:** nothing technical. The Description is the only place a
member learns what the bot is for, and a bot with a grey default avatar reads as
spam to anyone who did not install it.

---

## Step 3 -- Public Bot OFF

Bot -> **Public Bot** -> OFF -> Save Changes.

Only you (the app owner) can then add it to a server. Marginalia is
single-guild by design: it stores one book's text locally and gates access on
cohort membership in one `GUILD_ID`. A second guild would read the first
guild's book.

**BREAKS IF SKIPPED:** not a crash, an exposure. The invite URL carries no
secret, so anyone who sees it in a screenshot or a commit can add your bot to
their server, where it will dutifully create roles and threads.

Also confirm **Requires OAuth2 Code Grant** is OFF. On, the plain `scope=bot`
invite cannot complete -- you click Authorize, it fails or redirects to a blank
page, and the bot never joins. It looks like a bad URL; it is this toggle.

---

## Step 4 -- Privileged Gateway Intents

Bot -> Privileged Gateway Intents. Three toggles, exactly:

| Intent | Set to | Why |
|---|---|---|
| SERVER MEMBERS | **ON** | `bot.py:40` sets `members=True` |
| MESSAGE CONTENT | **OFF** | `bot.py:43` sets it False explicitly |
| PRESENCE | **OFF** | `bot.py:44` sets it False explicitly |

Flip Server Members ON, **Save Changes**, then RELOAD the page and confirm it
stuck. The toggle animates without persisting.

**BREAKS IF SKIPPED -- Server Members OFF.** The most deceptive failure in
Discord bot development, because it is not an error at all. The gateway simply
stops sending member data:

- `guild.members` reads **empty**.
- `role.members` reads **empty**, so `/roster` reports zero for a cohort of
  twenty.
- `on_member_join` **never fires**.
- `guild.get_member(user_id)` returns `None` for members who are really there,
  so `/cycle-close` skips every member instead of stripping the role
  (`club.py:332` guards on exactly that `None`).
- No exception, no warning, no log line, nothing wrong-looking in the portal.

There is one loud variant, and it is the good case: intent off in the portal
while the code requests `members=True` closes the gateway with **code 4014** and
raises `discord.errors.PrivilegedIntentsRequired`. discord.py does not reconnect
after 4014, so the process exits instead of looping. If you get that crash on
first boot you skipped this step and you got lucky.

**Why MESSAGE CONTENT stays off:** Marginalia never reads message text. Slash
options, button clicks and modal fields all arrive as structured data inside the
interaction payload, gated on nothing. All 21 commands are slash commands; there
is no prefix command and no `on_message`. Turning it on gains nothing, puts the
app in scope for privileged-intent review at the verification threshold, and
widens a token leak from "can post in one channel" to "can read every message in
the server". Same reasoning for Presence.

---

## Step 5 -- Interactions Endpoint URL: leave it BLANK

General Information -> **Interactions Endpoint URL**. Do nothing. Confirm empty.
Not a placeholder, not `http://localhost`.

Marginalia uses the **gateway**: it holds an outbound WebSocket and interactions
arrive over it (`__main__.py` calls `bot.start(token)`). No inbound port, no TLS,
no hostname. Filling this field switches the whole app to HTTP interactions,
where Discord POSTs each one to your URL and you verify signatures with the
**Public Key** -- which is the complete answer to what the Public Key is for.
Nothing here ever reads it.

**BREAKS IF YOU FILL IT IN:** every slash command breaks at once and the log
stays clean. Discord stops delivering over the gateway and POSTs to your URL;
nothing is listening; the member sees **"The application did not respond"** on
every command while the bot shows a healthy connection and logs nothing, because
from its side no interaction ever arrived.

If you ever see universal "did not respond" with a connected bot, check this
field first.

---

## Step 6 -- Installation tab -> Install Link = `None`

Installation (folded into OAuth2 on older portals):

1. **Installation Contexts** -- tick **Guild Install**, UNTICK **User Install**.
2. **Install Link** -> **`None`**.
3. Save Changes.

**`None` is also the fix for a validation error.** A private app (step 3) that
tries to keep a Discord-Provided Link is rejected with *"Private application
cannot have a default authorization link"*. Setting Install Link to `None` is
what resolves it -- and it leaves the hand-built URL in step 8 as the single
source of truth.

**BREAKS IF SKIPPED:** with User Install on, someone can add Marginalia to their
own account, at which point its commands appear for them in *other* servers and
in DMs, where `interaction.guild` is not your club (or is `None`) and every
lookup misses -- a channel through which someone outside the club can poke at
commands that read book text. And with Install Link set to a Discord-Provided
Link whose default permissions are narrower than step 8's table, anyone
installing through it gets a bot with too few permissions, and the resulting
403s look exactly like a role-hierarchy problem.

---

## Step 7 -- The token

Bot -> **Reset Token**.

**Before you click:** have an SSH session open on the unRAID box, in
`<repo>/deploy`, with `.env` ready to edit. You are about to hold a secret for
as short a time as possible.

1. Reset Token -> confirm -> 2FA code if prompted.
2. The token appears **once**. Copy.
3. On the unRAID box, paste into `deploy/.env`:
   `DISCORD_TOKEN=<token, no quotes, no spaces>`
4. `chmod 600 .env`
5. Clear your clipboard (copy anything else).
6. Reload the portal page. It is masked forever now. That is expected.

Rules, all of which matter:

- Shown **exactly once**. No "show token" anywhere. Lose it and you reset again.
- Reset invalidates the old token **instantly** -- not on next login. Any running
  bot is disconnected and cannot reconnect. Plan resets for when you can
  immediately do steps 3-6.
- **NEVER on the work laptop.** That machine is corporate-managed: corporate
  backup, endpoint monitoring, remote wipe. This project is personal. The whole
  test suite runs with no token by design, so there is no development reason for
  one to be there.
- Never a chat, an issue, a log, a screenshot, or a **command line** -- the
  sneaky one, because `DISCORD_TOKEN=xxx python -m marginalia` puts it in shell
  history *and* the process list where any user can `ps` it.
- GitHub and Discord both scan for leaked tokens and auto-invalidate. A token has
  a recognisable three-segment shape. Push one -- even to a private repo, even in
  a commit you amend away -- and it dies, presenting as a network problem.
- `Config.__repr__` renders it `<redacted>` (`config.py:26`, tested). Safety net,
  not a licence.

**BREAKS IF SKIPPED:** loud. `python -m marginalia` with it empty exits **2**
with `missing or empty environment variables: DISCORD_TOKEN, ...`. A *wrong*
token gives `LoginFailure` / 401 at connect. One of the few steps that cannot
fail quietly.

---

## Step 8 -- Invite the bot

One line, no wrapping, no spaces:

```
https://discord.com/oauth2/authorize?client_id=1545535072151670824&scope=bot+applications.commands&permissions=17918872005632
```

Both scopes are required. `bot` puts it in the member list and creates its
integration role; `applications.commands` is what lets it register slash
commands. Omit the second and the bot joins with no commands, and `setup_hook`'s
sync fails rather than warns.

MEASURED against `discord.Permissions` on discord.py 2.7.1 in this repo's venv:

| API name | Client UI label | Bit | Integer | Used for |
|---|---|---:|---:|---|
| `view_channel` | View Channels | 10 | 1024 | see `#florilegium` at all |
| `send_messages` | Send Messages | 11 | 2048 | signup message, reminders, announcements |
| `manage_messages` | Manage Messages | 13 | 8192 | pinning (headroom -- see below) |
| `manage_roles` | **Manage Permissions** | 28 | 268435456 | create the monthly role, assign on `/join`, strip on `/cycle-close` |
| `manage_threads` | Manage Threads | 34 | 17179869184 | archiving (headroom -- see below) |
| `create_public_threads` | Create Public Threads | 35 | 34359738368 | open each checkpoint thread |
| `send_messages_in_threads` | Send Messages in Threads | 38 | 274877906944 | post inside a thread; separate from Send Messages |
| `create_events` | Create Events | 44 | 17592186044416 | `/schedule` creates guild scheduled events (`reading.py:302`) |
| | | | **17918872005632** | **sum -- the number in the URL** |

Verify the arithmetic yourself:

```python
discord.Permissions(view_channel=True, send_messages=True, manage_messages=True,
    manage_roles=True, manage_threads=True, create_public_threads=True,
    send_messages_in_threads=True, create_events=True).value   # 17918872005632
```

**MANAGE_ROLES is labelled "Manage Permissions" in the client.** There is no
"Manage Roles" checkbox to find -- Server Settings -> Roles -> Marginalia ->
Permissions, and the invite consent screen, both say *Manage Permissions*. Look
for the wrong label and you conclude it is missing, grant something adjacent, and
every role operation 403s.

**CREATE_EVENTS is bit 44, not MANAGE_EVENTS.** Event creation stopped being
satisfiable by `MANAGE_EVENTS` alone on 2026-02-23. discord.py 2.7.1's
`create_scheduled_event` docstring still says only `manage_events` and is
**stale** -- do not treat it as the permission spec. UNVERIFIED: that bit 44 is
*sufficient* against the live API cannot be proven offline. This is why step 0
says use a scratch guild.

**MENTION_EVERYONE (bit 17, `131072`) is DELIBERATELY EXCLUDED.** Do not add it.
Pinging a role does not need it, as long as the role is mentionable -- and
`club.py:310` creates every cohort role with `mentionable=True` precisely so one
`role.mention` notifies the cohort with no elevated permission. With bit 17
absent, "this bot can never ping everyone" is enforced by **Discord**: no bug, no
bad refactor and no compromised token can produce an `@everyone` ping, because
the API refuses it. `tests/test_invariants.py::test_invariant_5_mention_everyone_appears_nowhere`
guards the code side, but a convention is only as strong as the next edit. In a
book club, an accidental 3am `@everyone` is what gets a bot removed.

**Two permissions the integer does NOT carry**, both real dependencies:

| Permission | Bit | Integer | Needed by |
|---|---:|---:|---|
| `read_message_history` | 16 | 65536 | `/ballot-result` -> `channel.fetch_message()` (`club.py:367`) needs View Channel **and** this |
| `send_polls` | 49 | 562949953421312 | `/ballot` sends a `discord.Poll` (`club.py:154`) |

A bot's effective permissions are the **union** of every role it holds including
`@everyone`, and default `@everyone` normally grants both, which is why this
usually just works. UNVERIFIED for your server -- no offline check can tell.
If `@everyone` has been stripped, either grant them on the channel (step 14) or
invite with `580868825492480` instead (MEASURED: the same eight plus these two).

**Perform the invite:** open the URL in the browser logged into the owning
account -> pick the **throwaway guild** -> read the consent tick-boxes (View
Channels, Send Messages, Manage Messages, **Manage Permissions**, Manage
Threads, Create Public Threads, Send Messages in Threads, Create Events; nothing
about mentioning everyone, nothing about administrator) -> Authorize -> confirm
`Marginalia` appears in the member list with an APP tag.

**BREAKS IF THE INTEGER IS WRONG:** the bot joins with whatever you actually
sent, and every operation outside that set 403s later -- when a member runs a
command, not at invite time. That is why you read the consent screen. A missing
tick-box means the URL got wrapped or an `&` was stripped; fix and re-run,
re-authorizing is harmless and updates the integration role.

Headroom note: `manage_messages` and `manage_threads` are in the integer but no
call site uses them today (`grep -rn '\.pin()\|archived=' marginalia/` -> no
hits). They are granted so pinning and archiving can land without re-inviting,
since changing permissions requires re-authorization.

---

## Step 9 -- Developer Mode ON

Client -> gear by your name (User Settings) -> **Advanced** -> **Developer Mode**
ON. No save button.

**BREAKS IF SKIPPED:** **Copy ID** does not exist in any right-click menu, so you
cannot obtain the IDs step 10 needs and there is no reasonable alternative.
People lose real time here, because a missing menu entry does not suggest a
setting exists to enable it.

---

## Step 10 -- Copy the guild and channel IDs into `.env`

1. Right-click the server icon -> **Copy Server ID** -> `GUILD_ID=`
2. Right-click **#florilegium** -> **Copy Channel ID** -> `BOOK_CLUB_CHANNEL_ID=`

All four required vars (`.env.example` is the authority):
`DISCORD_TOKEN`, `GUILD_ID`, `BOOK_CLUB_CHANNEL_ID`, `MARGINALIA_DB`. On unRAID
compose sets `MARGINALIA_DB` for you -- see `deploy/README.md`.

Two ID gotchas:

- These are **snowflakes**: 17-20 digits, larger than 2^53. Never route one
  through a spreadsheet or a JSON tool that might treat it as a float -- it
  silently loses its last digits. Paste text to text.
- Decimal ASCII digits only. `config.load` rejects anything else with
  `GUILD_ID must be a decimal snowflake id`.

**BREAKS IF SKIPPED:** loudly -- exit **2**, naming exactly what is missing.

**The WRONG value is the silent case.** A `GUILD_ID` for a server the bot is not
in means the sync targets an invisible guild: **zero commands appear, no error**.
A wrong `BOOK_CLUB_CHANNEL_ID` means `/cycle-open` refuses in the channel you
actually use, while reminders and threads target a channel nobody reads. Copy and
paste; do not retype.

The error messages never quote the offending **value**, only the variable name --
deliberate, so a token pasted into the `GUILD_ID` slot cannot reach a log.

---

## Step 11 -- Organizer commands: nothing to configure

There is no organizer role and no organizer env var in this build. The eight
organizer commands (`/cycle-open`, `/cycle-close`, `/ballot`, `/ballot-result`,
`/schedule`, `/meeting`, `/ingest`, `/purge_book`) carry
`@app_commands.default_permissions()` -- empty, i.e. Manage Server by default --
and that is the only gate.

**Worth knowing:** `default_permissions()` is a **default**. A guild
administrator can re-grant any command under Server Settings -> Integrations ->
Marginalia -> Command permissions, and there is no runtime role re-check behind
it. If you need a second layer, that is a code change, not a portal setting.

---

## Step 12 -- Confirm the integration role

Server Settings -> **Roles** -> **Marginalia**. It was created by the invite, is
*managed* (you cannot delete, rename, or assign it to a human), and its ticked
permissions must match step 8's table -- remembering MANAGE_ROLES shows as
**Manage Permissions**.

**BREAKS IF SKIPPED:** a mismatch here means the invite's `permissions=` was
wrong or truncated. Better to learn it now than from a 403 during a member's
first `/join`.

---

## Step 13 -- ROLE HIERARCHY: drag Marginalia ABOVE the cohort roles

```
+========================================================================+
||                                                                      ||
||   THE NUMBER ONE CAUSE OF 403 IN THIS ENTIRE PROJECT.                ||
||                                                                      ||
||   Server Settings -> Roles -> drag "Marginalia" ABOVE every          ||
||   "Marginalia YYYY-MM" cohort role.                                  ||
||                                                                      ||
||   Skip it and EVERY role assignment returns 403, and the error text   ||
||   never says why.                                                    ||
||                                                                      ||
+========================================================================+
```

Server Settings -> Roles is a vertical ordered list; higher means higher.

1. Grab the `Marginalia` row by its `::` grip.
2. Drag it **up**, above every `Marginalia YYYY-MM` role -- **including ones that
   do not exist yet**, which is why you want headroom.
3. Release. Order saves immediately (click the Save bar if one appears).
4. Read the list top to bottom:

```
(admin / owner roles)
Marginalia            <-- the bot's managed role
Marginalia 2026-10
Marginalia 2026-09
...
@everyone
```

**The rule:** a bot can only add, remove, create, edit or delete a role
**strictly below** its own highest role. Not equal to. This is a hard API rule
and no permission overrides it -- granting Administrator does **not** help.

**BREAKS IF SKIPPED:** `member.add_roles(role)` raises `discord.Forbidden` --
HTTP 403, code `50013`, **"Missing Permissions"**. That message is a lie of
omission: the permission IS granted, the *hierarchy* is wrong. Nothing in the
response, the traceback, or the portal mentions position. You will re-read step
8, re-tick Manage Permissions, re-invite the bot, and none of it will change
anything, because the problem is a drag-and-drop you have not done.

Concretely: `/join` fails for everyone, `/leave` fails, `/cycle-open` cannot
create the role at all, and `/cycle-close` cannot strip it -- the code catches
`Forbidden` and replies with `club.HIERARCHY_HELP`, which names this cause.

**On any 403 touching a role, check this step FIRST.** It is right about nine
times in ten.

---

## Step 14 -- Channel overwrites on #florilegium

Right-click **#florilegium** -> Edit Channel -> **Permissions**.

1. Under Roles/Members, `+` and add the **Marginalia** role if absent.
2. Set these to the green tick (**explicit allow**) -- not grey, never red:
   View Channel, Send Messages, Manage Messages, Create Public Threads,
   Send Messages in Threads, Manage Threads, **Read Message History**,
   **Create Polls**. The last two are step 8's "does not carry" pair; ticking
   them here is the cheapest way to stop depending on your `@everyone` config.
3. Check the **@everyone** overwrite too. Members need View Channel and Read
   Message History here or they cannot see the signup message or the threads --
   which looks like a bot bug and is not one.
4. Save Changes.

**How a channel deny silently beats a guild grant.** Discord resolves in layers:
guild role permissions, then the channel's `@everyone` overwrite, then channel
role overwrites, then member-specific. **An explicit deny at channel level wins
over a guild-level grant.** The only exception is Administrator, which Marginalia
deliberately lacks. So this is possible and entirely broken:

- Server Settings -> Roles -> Marginalia: Create Public Threads **ticked**
- `#florilegium` -> Permissions -> Marginalia: Create Public Threads **red X**

The role page says granted. Thread creation 403s anyway. If a permission looks
granted but behaves denied, you are looking at the wrong layer -- check the
channel overwrites, and the **category** too, since a channel synced to its
category inherits the category's.

Prefer grey (neutral, inherit) over red for anything you do not actively want to
forbid: neutral lets a guild grant through, red does not.

**BREAKS IF SKIPPED:** 403s that name no cause -- no thread, no message, or a
poll that cannot be read back. The worst variant is a deny on View Channel: the
bot then behaves as though `BOOK_CLUB_CHANNEL_ID` points at a nonexistent
channel, because `get_channel` returns `None`.

---

## Step 15 -- Verification, in the throwaway guild

Each item says what CORRECT looks like, so partial is recognisable as partial.

1. **Boot.** `docker logs -f marginalia` (or `python -m marginalia`). Correct:
   process stays up, one ready line, no traceback.
   - `missing or empty environment variables` -> step 7 or 10.
   - `PrivilegedIntentsRequired` / close 4014 -> step 4, you did not Save.
   - `LoginFailure` / 401 -> wrong token, reset after copying, or leak-scanned.
   - Any `ExtensionError` traceback -> a cog failed to import. **`setup_hook`
     now refuses to boot** rather than syncing a reduced tree. That is
     deliberate: the old behaviour logged a warning and synced anyway, leaving
     eight commands absent from Discord with the bot looking green. Fix the
     import; it is a code problem, not a portal one.
2. **Commands appear INSTANTLY.** Type `/`. Correct: **21** top-level entries,
   immediately -- `/progress` is a group, so 22 runnable. Guild-scoped
   registration propagates instantly; only global commands take an hour and this
   bot registers none. Zero commands means a wrong `GUILD_ID` (step 10) or a
   missing `applications.commands` scope (step 8). Do NOT loop the sync -- the
   cap is 200 command creates per day per guild.
3. **`/cycle-open` outside #florilegium refuses.** Correct: an ephemeral refusal
   naming the configured channel, and **no role is created** -- the guard sits
   before `create_role` so a misfire leaves no orphan. Then run it inside
   `#florilegium` as a positive control; without both halves, an
   always-refusing bug looks identical to a working guard.
4. **The Join button survives a restart.** `/cycle-open`, click Join, get the
   role. Then fully stop and start the bot and click the **same old message's**
   button. Correct: it still works. "This interaction failed" means the
   `DynamicItem` was not re-registered -- `add_dynamic_items` missing from
   `setup_hook`, an instance passed instead of the class, or a `custom_id` not
   matching the template. All code, not portal. Note the same message also
   appears for any unhandled callback exception and for blowing the 3-second
   deadline without deferring; a *registration* failure logs nothing at all,
   which is how you tell them apart.
5. **Role pings actually NOTIFY.** Needs a second account with the cohort role
   and the channel unmuted. Test **both** paths: the poller (`bot.deliver`) and
   the interaction handler (`/cycle-open`, `/roster`). Correct: a real
   notification -- badge, sound -- not merely a blue role pill. Interaction
   responses default to parsing **users only**, so a role mention notifies
   nobody with no error and nothing in the log; `club._reply` and `bot.deliver`
   both pass explicit `AllowedMentions(everyone=False, users=False,
   roles=[role])`. UNVERIFIED against the live API. A silent no-ping is the
   entire reminder feature failing quietly.
6. **Timestamps render per-member.** `/next` or `/schedule`. Correct: each
   reader sees their own local time from one identical message. A raw number or
   literal `<t:...>` means the markup was escaped or placed where Discord does
   not render it. **Needs two humans in two zones** -- an agent cannot do this,
   and one person switching their own client timezone is a weaker test.
7. **A reminder survives a stop across its due time.** Set one 3-4 minutes out,
   stop the bot, wait past due, start. Correct: delivered once, late, not twice
   and not never.
8. **Threads come out PUBLIC** and your second account can see them without
   being added. discord.py 2.7.1 defaults `create_thread` to
   **private_thread**; `reading.py:63` passes `public_thread` explicitly. A lock
   icon means someone omitted `type=` -- report it, do not paper over it with
   permissions.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 403 on any role operation; code `50013` "Missing Permissions" | **Role hierarchy** -- Marginalia sits at or below the cohort role. Administrator does not override it. | **Step 13.** Then: MANAGE_ROLES is labelled "Manage Permissions" (step 8). Then: a channel deny (step 14). |
| Zero commands when you type `/` | `GUILD_ID` names a server the bot is not in, or the invite omitted `applications.commands`, or `setup_hook` raised before the sync | Re-copy the Server ID (step 10); re-invite with the full URL (step 8); read the log. Guild-scoped is instant, so waiting is not the answer. |
| Bot boots then exits with an `ExtensionError` traceback | A cog failed to import. Fail-fast is deliberate. | Fix the import. Not a portal problem. Do not restore a try/except -- that is what silently shipped a reduced command tree. |
| "This interaction failed" on an old button after restart | `DynamicItem` not re-registered, or `custom_id` does not match the template | Confirm `setup_hook` calls `add_dynamic_items` with the **class**; an instance raises `TypeError: issubclass() arg 1 must be a class`. |
| "The application did not respond" on everything, bot looks connected | **Interactions Endpoint URL is set.** Discord POSTs to a URL nothing serves. | **Step 5.** Clear the field. An empty log plus universal failure is this field's signature. |
| Role mention renders blue, pings nobody | (a) interaction path without explicit `allowed_mentions`; (b) the role is not mentionable | (a) check the call site, not the portal. (b) confirm you are pinging the current cohort's role. Do NOT "fix" it by adding MENTION_EVERYONE (step 8). |
| Close code 4014 / `PrivilegedIntentsRequired` | Server Members Intent not granted | **Step 4.** Enable, Save, reload to confirm. No reconnect after 4014 -- exiting is intentional. |
| Close code 4013 | Invalid intents bitfield -- library/API mismatch or hand-rolled intents | Do not hand-build intents. Confirm discord.py is **2.7.1**. |
| Connects, roster reads zero, no error anywhere | Server Members Intent off | **Step 4.** If the toggle is on and it is still empty, it is a query bug, not a portal one. |
| `/cycle-open` refuses in the channel you actually use | `BOOK_CLUB_CHANNEL_ID` points elsewhere | Re-copy the #florilegium Channel ID (step 10). |
| Bot logs in but the club channel "does not exist" | Wrong `BOOK_CLUB_CHANNEL_ID`, **or** View Channel denied at channel level -- `get_channel` returns `None` either way | Check both (steps 10 and 14). Indistinguishable from the log alone. |
| `/ballot` cannot post the poll | `send_polls` (bit 49) not in the invite integer, normally inherited from `@everyone` | Grant **Create Polls** on the channel (step 14) or re-invite with `580868825492480`. |
| `/ballot-result` cannot read the poll back | `read_message_history` (bit 16) likewise | Same fix. |
| Scheduled-event creation 403s | `create_events` (bit 44) missing. Required since 2026-02-23; discord.py's docstring naming `manage_events` is stale. | Confirm bit 44 is in the integer (`17918872005632` has it), or tick **Create Events** by hand. Also check the guild's 100 scheduled-or-active event cap. |
| Everything 403s, then all requests fail | You tripped the invalid-request ceiling: ~10,000 per 10 minutes ends in a temporary Cloudflare IP ban on the host. Usually a retry loop on a 403. | Stop the bot. Wait it out. Fix the 403 (nearly always step 13) before restarting. This code never retries `Forbidden`/`NotFound` (invariant 4), so a loop points at something new. |
| Worked yesterday, cannot log in today | Token reset, or auto-invalidated after being detected somewhere public | Below. |

---

## If the token leaks

Assume full compromise. A token in a chat, screenshot, commit, pasted log, CI
artifact, or on a machine you no longer control **is** compromised. Do not
reason about how unlikely it is that someone noticed.

1. Portal -> Marginalia -> **Bot** -> **Reset Token** -> confirm (2FA if asked).
2. Copy once. Same rules as step 7.
3. Paste into `.env` on the unRAID box, replacing the old value.
4. `chmod 600 .env`
5. `docker compose up -d` (or `docker restart marginalia`).

**The reset is immediate and total** -- no grace period, no overlap. Every
process holding the old token, including your own running bot, is disconnected at
once. Expect downtime between steps 1 and 5, so reset only when you can finish.

Afterwards:

- Find how it escaped and close that path. A rotated token leaking the same way
  is not progress.
- If it went into git: `commit --amend` and force-push do **not** remove it. The
  blob survives in the reflog, in every clone, and in GitHub's dangling-object
  storage. The reset is the fix; history rewriting is hygiene.
- **Nothing else needs rotating.** The Application ID and Public Key are not
  secrets. The token is the only secret this bot has.
- Read Server Settings -> **Audit Log** for anything the bot did that you did not
  ask for. Step 8's narrow permission set is exactly what bounds that list: no
  Administrator, no Manage Server, no MENTION_EVERYONE, no message content.

---

## What this document could NOT verify

Every item needs a live gateway. Nothing here has ever held a token.

| Claim | Status |
|---|---|
| `17918872005632` is the sum of those eight flags | **MEASURED** against discord.py 2.7.1's own bitfield |
| `mention_everyone` = 131072 and is excluded | **MEASURED** |
| `read_message_history` = 65536, `send_polls` = 562949953421312, neither in the base integer | **MEASURED** |
| `580868825492480` = the eight plus those two | **MEASURED** |
| 4014 raises `PrivilegedIntentsRequired`; 4013/4014 do not reconnect | **MEASURED** from library source |
| `create_thread` defaults to **private_thread** | **MEASURED** |
| `create_scheduled_event`'s docstring names only `manage_events` and is stale | **MEASURED** |
| `recurrence_rule` does not exist in 2.7.1 | **MEASURED** |
| Timestamp markup carries **seconds**, not milliseconds | **MEASURED** |
| 21 top-level / 22 runnable commands, 3 cogs | **MEASURED** by loading all three cogs offline against a real `Marginalia` |
| `create_events` (bit 44) is *sufficient* on the live API | **UNVERIFIED** -- test in the throwaway guild |
| A role mention from the interaction path really notifies | **UNVERIFIED** -- the most valuable check on the list |
| An ephemeral response really suppresses others' push notifications | **UNVERIFIED** -- the whole spoiler design rests on it |
| The persistent Join button survives a restart | **UNVERIFIED** |
| Threads come out public in practice | **UNVERIFIED** (the explicit `type=` is code; the outcome is not) |
| Guild-scoped sync lands and all 21 appear instantly | **UNVERIFIED** |
| `default_permissions()` really hides organizer commands in the client | **UNVERIFIED** |
| Whether your `@everyone` grants Read Message History and Create Polls | **UNVERIFIED** -- depends on your server, not on any code |

Related: `docs/HANDOFF.md` (the laptop-to-server boundary),
`docs/ENV.md` (every measured discord.py fact),
`deploy/README.md` (the unRAID runbook), `.env.example` (exact variable names).
