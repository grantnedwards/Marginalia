# ENV.md -- measured environment facts

Everything in this file was produced by running a command on this machine on
2026-09-04. Nothing here is inferred from documentation or from the design
notes. Where a measurement contradicts what planning assumed, the line is
prefixed with `CORRECTION:`. Those lines are the ones worth reading twice.

This file is authoritative over the design notes for anything it covers.

## 0. Interpreter and venv

```
$ /opt/homebrew/bin/python3.13 -V
Python 3.13.11

$ /opt/homebrew/bin/python3.13 -m venv /Users/granteds/marginalia/.venv
$ /Users/granteds/marginalia/.venv/bin/python -c "import sys; print(sys.executable); print(sys.version)"
/Users/granteds/marginalia/.venv/bin/python
3.13.11 (main, Dec  5 2025, 16:06:33) [Clang 17.0.0 (clang-1700.4.4.1)]

$ /Users/granteds/marginalia/.venv/bin/python -m pip install --upgrade pip
Successfully installed pip-26.2.1
```

Every command in the rest of this file is run with
`/Users/granteds/marginalia/.venv/bin/python` (written below as `python`) or
`/Users/granteds/marginalia/.venv/bin/pip` (written as `pip`).

## 1. audioop on Python 3.13 -- the boot-crash risk. CONFIRMED REAL.

The stdlib module is gone, exactly as PEP 594 says:

```
$ python -c "import audioop"
ModuleNotFoundError: No module named 'audioop'
```

`import discord` genuinely depends on it. Proof, with `audioop` blocked by a
`sys.meta_path` finder so the installed shim cannot satisfy it:

```
$ python -c "
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'audioop' or name.startswith('audioop.'):
            raise ModuleNotFoundError(\"No module named 'audioop'\", name=name)
        return None
sys.meta_path.insert(0, Block())
import discord
"
RESULT: import discord FAILED -> ModuleNotFoundError: No module named 'audioop'
    from .voice_client import VoiceClient, VoiceProtocol
  File ".../site-packages/discord/voice_client.py", line 35, in <module>
    from .player import AudioPlayer, AudioSource
  File ".../site-packages/discord/player.py", line 30, in <module>
    import audioop
```

So the import chain is `discord/__init__.py` -> `voice_client.py:35` ->
`player.py:30` -> `import audioop`. It is an unconditional top-level import.
There is no way to avoid it by not using voice. **audioop-lts is MANDATORY on
Python 3.13+ for this project.** Without it the bot dies on the first line of
`import discord`, before any of our code runs.

CORRECTION: planning treated installing the shim as a separate manual step we
would have to discover. It is not. discord.py 2.7.1 declares the shim itself,
so a plain `pip install discord.py==2.7.1` already pulls it in:

```
$ python -c "import importlib.metadata as md; print([r for r in md.requires('discord.py')])"
  aiohttp<4,>=3.7.4
  audioop-lts; python_version >= "3.13"
  ...
```

```
$ pip install "discord.py==2.7.1" aiosqlite lxml python-dotenv pytest pytest-asyncio ruff
Successfully installed ... audioop-lts-0.2.2 discord.py-2.7.1 ...
```

Note the marker is `python_version >= "3.13"`, i.e. the shim is *not* installed
on 3.12 or lower. It is still pinned explicitly in `requirements.txt` so the
target install can never resolve without it. Confirmed present in the built
x86_64 image (see 14a).

```
$ python -c "
import discord, audioop, importlib.metadata as md
print('import discord OK'); print(discord.__version__)
print(audioop.__file__); print(md.version('audioop-lts'))"
import discord OK
2.7.1
/Users/granteds/marginalia/.venv/lib/python3.13/site-packages/audioop/__init__.py
0.2.2
```

Practical consequence for the deploy agent: do not "trim" audioop-lts out of
requirements.txt on the grounds that the bot has no voice features. It has
nothing to do with voice features being used.

## 2. discord.py version

```
$ python -c "import discord; print(discord.__version__); print(discord.version_info)"
2.7.1
VersionInfo(major=2, minor=7, micro=1, releaselevel='final', serial=0)
```

## 3. discord.Poll

It exists. The constructor is not what planning assumed.

```
$ python -c "import discord, inspect; print(inspect.signature(discord.Poll.__init__))"
(self, question: 'Union[PollMedia, str]', duration: 'datetime.timedelta', *, multiple: 'bool' = False, layout_type: 'PollLayoutType' = <PollLayoutType.default: 1>) -> 'None'
```

CORRECTION: there is no `allow_multiselect` parameter. The keyword is
`multiple`. Passing the planning name is a hard `TypeError`:

```
$ python -c "
import discord, datetime
discord.Poll(question='q', duration=datetime.timedelta(hours=24), allow_multiselect=True)"
TypeError: Poll.__init__() got an unexpected keyword argument 'allow_multiselect'
```

`multiple` works and round-trips:

```
$ python -c "
import discord, datetime
p = discord.Poll(question='Which book?', duration=datetime.timedelta(hours=24), multiple=True)
print(p.multiple)"
True
```

Also note `question` and `duration` are POSITIONAL_OR_KEYWORD, not
keyword-only; only `multiple` and `layout_type` are keyword-only. `duration`
is a `datetime.timedelta`, not an int of hours.

Answers are added afterwards, one at a time, fluent style:

```
$ python -c "import discord, inspect; print(inspect.signature(discord.Poll.add_answer))"
(self, *, text: 'str', emoji: 'Optional[Union[PartialEmoji, Emoji, str]]' = None) -> 'Self'
```

### Maximum answer count

CORRECTION: **the library enforces no maximum at all.** Planning expected the
constructor or `add_answer` to cap answers at 10. It does not cap anything:

```
$ python -c "
import discord, datetime
p = discord.Poll(question='Which book?', duration=datetime.timedelta(days=7))
for i in range(1, 13):
    p.add_answer(text='answer %d' % i); print(i, len(p.answers))"
1 1
2 2
...
10 10
11 11
12 12
```

Reading the source confirms there is no guard -- `add_answer` raises only
`ClientException('Cannot append answers to a poll that is active')` when the
poll already has a message attached, and otherwise appends unconditionally
with `id=len(self.answers) + 1`.

CORRECTION: nor is there any client-side length or duration validation, even
though the docstrings state the limits (question 300 chars, answer text 55
chars):

```
$ python -c "
import discord, datetime
p = discord.Poll(question='x'*400, duration=datetime.timedelta(hours=1))
p.add_answer(text='y'*200)
print(len(p.question), len(p.answers[0].text))
for h in (0, 169, 1000):
    print(h, discord.Poll(question='q', duration=datetime.timedelta(hours=h)).duration)"
400 200
0 0:00:00
169 7 days, 1:00:00
1000 41 days, 16:00:00
```

Downstream consequence, and this matters: **whichever module builds the poll
must do its own validation.** Cap at 10 answers, 300 chars of question, 55
chars of answer text, and 1..168 hours of duration before calling `send`.
Otherwise the failure mode is an HTTP 400 from Discord at send time -- a
runtime error against a live API, which is exactly the class of bug this
project cannot test locally. The numeric limits above are quoted from
discord.py's own docstrings (`poll.py` lines 331 and 605); the API-side
rejection behaviour is NOT verifiable on this machine because there is no
token here.

`poll=` is accepted by all three send paths:

```
$ python -c "
import discord, inspect
for t in (discord.abc.Messageable.send, discord.InteractionResponse.send_message, discord.Webhook.send):
    print('poll' in inspect.signature(t).parameters)"
True
True
True
```

`Poll.end()` takes no arguments (`(self) -> 'Self'`), and `Message.poll`
exists for reading a poll back off a fetched message.

## 4. discord.ui.DynamicItem

It exists.

```
$ python -c "
import discord, inspect
print(hasattr(discord.ui, 'DynamicItem'))
print(inspect.signature(discord.ui.DynamicItem.__init__))
print(inspect.signature(discord.ui.DynamicItem.__init_subclass__))
print(inspect.signature(discord.ui.DynamicItem.from_custom_id))
print(inspect.signature(discord.Client.add_dynamic_items))"
True
(self, item: 'BaseT', *, row: 'Optional[int]' = None) -> 'None'
(*, template: 'Union[str, re.Pattern[str]]') -> 'None'
(interaction: 'Interaction[ClientT]', item: 'Item[Any]', match: 're.Match[str]', /) -> 'Self'
(self, *items: 'Type[DynamicItem[Item[Any]]]') -> 'None'
```

So: `template` is a required keyword-only *class* argument typed
`Union[str, re.Pattern[str]]`. A `str` is compiled for you. Omitting it is a
hard error:

```
$ python -c "
import discord
class T3(discord.ui.DynamicItem[discord.ui.Button]):
    async def callback(self, i): ...
"
TypeError: DynamicItem.__init_subclass__() missing 1 required keyword-only argument: 'template'
```

`add_dynamic_items` takes the **class**, not an instance. Docstring, verbatim:
"Registers :class:`~discord.ui.DynamicItem` classes for persistent listening.
This method accepts *class types* rather than instances." Passing an instance
fails, though not with the documented friendly error:

```
$ python -c "... c.add_dynamic_items(vote_instance)"
TypeError: issubclass() arg 1 must be a class
```

CORRECTION: `DynamicItem.template` is a **property**, so reading it off the
class gives you a `property` object, not a pattern. The compiled pattern is
the class var `__discord_ui_compiled_template__`. Read `.template` on an
*instance*:

```
$ python -c "
import discord
class Vote(discord.ui.DynamicItem[discord.ui.Button],
           template=r'mgl:vote:(?P<cohort>\d+):(?P<choice>[a-z]+)'):
    def __init__(self, cohort, choice):
        super().__init__(discord.ui.Button(label=choice, custom_id=f'mgl:vote:{cohort}:{choice}'))
    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['cohort']), match['choice'])
    async def callback(self, interaction): ...
print(type(Vote.template).__name__)
print(repr(Vote.__discord_ui_compiled_template__))
v = Vote(7, 'yes')
print(repr(v.template))
print(v.template.match(v.custom_id).groupdict())
print(v.custom_id, len(v.custom_id), v.is_dispatchable(), v.is_persistent())"
property
re.compile('mgl:vote:(?P<cohort>\\d+):(?P<choice>[a-z]+)')
re.compile('mgl:vote:(?P<cohort>\\d+):(?P<choice>[a-z]+)')
{'cohort': '7', 'choice': 'yes'}
mgl:vote:7:yes 14 True True
```

The constructor validates the wrapped item's `custom_id` against the template
immediately, which is a useful early failure:

```
$ python -c "
import discord
class Bad(discord.ui.DynamicItem[discord.ui.Button], template=r'^ok:(?P<a>\d+)$'):
    async def callback(self, i): ...
Bad(discord.ui.Button(label='b', custom_id='nope'))"
ValueError: item custom_id 'nope' must match the template '^ok:(?P<a>\\d+)$'
```

Named groups are what `from_custom_id` receives as `match`, so use
`(?P<name>...)` groups, not positional ones. `match` is positional-only.

## 5. Threads: ChannelType.public_thread and create_thread's default

`public_thread` exists.

```
$ python -c "
import discord
print([m.name for m in discord.ChannelType])
print(discord.ChannelType.public_thread.value, discord.ChannelType.private_thread.value, discord.ChannelType.news_thread.value)"
['text', 'private', 'voice', 'group', 'category', 'news', 'news_thread', 'public_thread', 'private_thread', 'stage_voice', 'forum', 'media']
11 12 10
```

`TextChannel.create_thread` -- every parameter is keyword-only except `self`:

```
name                  KEYWORD_ONLY  (required)
message               KEYWORD_ONLY  default None
auto_archive_duration KEYWORD_ONLY  default MISSING
type                  KEYWORD_ONLY  default None
reason                KEYWORD_ONLY  default None
invitable             KEYWORD_ONLY  default True
slowmode_delay        KEYWORD_ONLY  default None
```

CONFIRMED, planning was right: the signature default for `type` is `None`, and
the library body then hardcodes private:

```python
        if type is None:
            type = ChannelType.private_thread
```

Docstring, verbatim: "The type of thread to create. If a ``message`` is passed
then this parameter is ignored, as a thread created with a message is always a
public thread. By default this creates a private thread if this is ``None``."

Two operational consequences:

1. Any caller that wants a public thread **must pass
   `type=discord.ChannelType.public_thread` explicitly.** Omitting it silently
   creates a private thread that most members cannot see -- a silent
   wrong-behaviour bug, not a crash.
2. If you pass `message=<some message>` you get a public thread regardless of
   `type`, because the library routes to `start_thread_with_message` and never
   sends the type field at all.

Permissions per the same docstring, verbatim: "To create a public thread, you
must have :attr:`~discord.Permissions.create_public_threads`. For a private
thread, :attr:`~discord.Permissions.create_private_threads` is needed
instead."

## 6. Guild.create_scheduled_event

```
$ python -c "
import discord, inspect
sig = inspect.signature(discord.Guild.create_scheduled_event)
print(list(sig.parameters))
print('recurrence_rule' in sig.parameters)"
['self', 'name', 'start_time', 'entity_type', 'privacy_level', 'channel', 'location', 'end_time', 'description', 'image', 'reason']
False
```

All of `name` onward are keyword-only. `name` and `start_time` are required;
`entity_type`, `privacy_level`, `channel`, `location`, `end_time`,
`description`, `image` default to `MISSING`; `reason` defaults to `None`.

CORRECTION: **`recurrence_rule` is not supported anywhere in this version.**
Not on create, not on edit, and there is no type to build one with:

```
$ python -c "
import discord, inspect
print([n for n in dir(discord.ScheduledEvent) if 'recur' in n.lower()])
print([n for n in dir(discord) if 'recur' in n.lower()])
print('recurrence_rule' in inspect.signature(discord.ScheduledEvent.edit).parameters)"
[]
[]
False
```

Any design that leaned on recurring Discord scheduled events for the monthly
cohort cadence has to be reworked: either create a fresh one-shot event each
month from our own scheduler, or drop scheduled events and rely on the
reminder path.

### Docstring on required permissions -- verbatim

The relevant line of `inspect.getdoc(discord.Guild.create_scheduled_event)`,
copied exactly:

```
You must have :attr:`~Permissions.manage_events` to do this.
```

The `Raises` section adds, verbatim:

```
Forbidden
    You are not allowed to create scheduled events.
```

CORRECTION: planning was right that this docstring is stale. discord.py 2.7.1
still says `manage_events`, and says nothing about `create_events`. Do not
treat the docstring as the permission spec. The bot's invite scope should
include `create_events` as well; the permission integer in section 7 already
includes it. This staleness is a documentation fact about the library, and the
actual API-side requirement is NOT verifiable on this machine (no token), so it
stands on the planning note, not on a measurement here.

## 7. Permissions flag integers

```
$ python -c "
import discord
for f in ['view_channel','send_messages','manage_messages','manage_roles','manage_threads',
          'create_public_threads','send_messages_in_threads','create_events','mention_everyone']:
    v = getattr(discord.Permissions, f).flag
    print('%-26s = %d   (0x%x, bit %d)' % (f, v, v, v.bit_length()-1))"
view_channel               = 1024   (0x400, bit 10)
send_messages              = 2048   (0x800, bit 11)
manage_messages            = 8192   (0x2000, bit 13)
manage_roles               = 268435456   (0x10000000, bit 28)
manage_threads             = 17179869184   (0x400000000, bit 34)
create_public_threads      = 34359738368   (0x800000000, bit 35)
send_messages_in_threads   = 274877906944   (0x4000000000, bit 38)
create_events              = 17592186044416   (0x100000000000, bit 44)
mention_everyone           = 131072   (0x20000, bit 17)
```

Sum of all except `mention_everyone`, shown as a running total:

```
  + view_channel                             1024  -> running total 1024
  + send_messages                            2048  -> running total 3072
  + manage_messages                          8192  -> running total 11264
  + manage_roles                        268435456  -> running total 268446720
  + manage_threads                    17179869184  -> running total 17448315904
  + create_public_threads             34359738368  -> running total 51808054272
  + send_messages_in_threads         274877906944  -> running total 326685961216
  + create_events                  17592186044416  -> running total 17918872005632

SUM = 17918872005632
Expected 17918872005632 ? True
difference (actual - expected) = 0
```

Cross-checked against the library's own bitfield rather than hand arithmetic:

```
$ python -c "
import discord
subset = ['view_channel','send_messages','manage_messages','manage_roles','manage_threads',
          'create_public_threads','send_messages_in_threads','create_events']
print(discord.Permissions(**{f: True for f in subset}).value)"
17918872005632
```

CONFIRMED: **17918872005632 is correct.** Planning's number matches exactly.
`mention_everyone` is 131072 and is deliberately excluded -- adding it would
give 17918872136704.

A few neighbours, recorded because they come up when writing the invite URL:

```
create_private_threads = 68719476736
read_message_history   = 65536
embed_links            = 16384
Intents.guilds          = 1
Intents.members         = 2
Intents.message_content = 32768
```

## 8. discord.utils.format_dt

```
$ python -c "import discord, inspect; print(inspect.signature(discord.utils.format_dt))"
(dt: 'datetime.datetime', /, style: 'Optional[TimestampStyle]' = None) -> 'str'
```

`dt` is positional-only. All nine style letters are accepted:

```
$ python -c "
import discord, datetime
dt = datetime.datetime(2026, 9, 4, 20, 30, 45, tzinfo=datetime.timezone.utc)
print(int(dt.timestamp()))
for s in 'tTdDfFsSR':
    print(s, discord.utils.format_dt(dt, style=s))
print('default', discord.utils.format_dt(dt))"
1788553845
t <t:1788553845:t>
T <t:1788553845:T>
d <t:1788553845:d>
D <t:1788553845:D>
f <t:1788553845:f>
F <t:1788553845:F>
s <t:1788553845:s>
S <t:1788553845:S>
R <t:1788553845:R>
default <t:1788553845>
```

CONFIRMED: nine letters `t T d D f F s S R`, and **the output is in SECONDS,
not milliseconds**:

```
extracted numeric token from <t:...:F> = 1788553845
equals int(dt.timestamp()) [SECONDS]?   True
equals int(dt.timestamp()*1000) [MILLIS]? False
```

Omitting `style` emits `<t:SECONDS>` with no trailing style segment (Discord
renders that as `f`). A non-UTC aware datetime for the same instant produces
the identical token, i.e. the function normalises to the epoch and the *user's
client* does the localisation:

```
$ python -c "
import discord, datetime
from zoneinfo import ZoneInfo
print(discord.utils.format_dt(datetime.datetime(2026,9,4,15,30,45, tzinfo=ZoneInfo('America/Chicago')), style='F'))"
<t:1788553845:F>
```

The docstring style table (verbatim descriptions) is: t Short Time, T Medium
Time, d Short Date, D Long Date, f (default) Long Date + Short Time, F Full
Date + Short Time, s Short Date + Short Time, S Short Date + Medium Time,
R Relative Time. The docstring also warns: "Note that the exact output depends
on the user's locale setting in the client."

## 9. discord.AllowedMentions

```
$ python -c "import discord, inspect; print(inspect.signature(discord.AllowedMentions.__init__))"
(self, *, everyone: bool = True, users: Union[bool, Sequence[Snowflake]] = True,
 roles: Union[bool, Sequence[Snowflake]] = True, replied_user: bool = True) -> None
```

Four keyword-only parameters: `everyone`, `users`, `roles`, `replied_user`.

CORRECTION (small but it will bite a test author): the `= True` that
`inspect.signature` prints is not the bool `True`. The real default is a module
sentinel `discord.mentions._FakeBool` that compares equal to `True` but is not
`True`:

```
$ python -c "
from discord.mentions import default
print(repr(default), type(default))
print(default == True, bool(default), default is True)"
True <class 'discord.mentions._FakeBool'>
True True False
```

So never assert `am.everyone is True` on a default-constructed
`AllowedMentions`; assert `== True` or assert on `to_dict()`.

`roles=` accepts a bool OR a sequence of role-like objects, confirmed both
ways. Objects are serialised to snowflake ids:

```
$ python -c "
import discord
class FakeRole:
    def __init__(self, i): self.id = i
r1, r2 = FakeRole(111111111111111111), FakeRole(222222222222222222)
print(discord.AllowedMentions(everyone=False, users=False, roles=[r1, r2], replied_user=False).to_dict())
o = discord.Object(id=333333333333333333)
print(discord.AllowedMentions(everyone=False, users=[o], roles=[o], replied_user=False).to_dict())
print(discord.AllowedMentions().to_dict())
print(discord.AllowedMentions.none().to_dict())
print(discord.AllowedMentions.all().to_dict())
print(discord.AllowedMentions(everyone=False, roles=False, users=True).to_dict())"
{'roles': [111111111111111111, 222222222222222222], 'parse': []}
{'users': [333333333333333333], 'roles': [333333333333333333], 'parse': []}
{'replied_user': True, 'parse': ['everyone', 'users', 'roles']}
{'parse': []}
{'replied_user': True, 'parse': ['everyone', 'users', 'roles']}
{'replied_user': True, 'parse': ['users']}
```

Anything with an `.id` attribute works -- the `Snowflake` annotation is a
protocol, not a nominal type, which means tests can use plain stubs and do not
need real `discord.Role` objects. `AllowedMentions.merge(other)` exists, taking
one `AllowedMentions`.

Note the asymmetry, straight from `to_dict` source: a bool `True` adds the name
to `parse`, a sequence goes into an explicit id list and the name is left out of
`parse`. `AllowedMentions.none()` is the safe default for anything the bot
echoes from user text or from an ebook.

## 10. aiosqlite

```
$ python -c "
import aiosqlite, sqlite3, importlib.metadata as md
print(aiosqlite.__version__, md.version('aiosqlite'))
print(hasattr(aiosqlite, 'Row'), aiosqlite.Row, aiosqlite.Row is sqlite3.Row)
print(aiosqlite.sqlite_version, sqlite3.sqlite_version)"
0.22.1 0.22.1
True <class 'sqlite3.Row'> True
3.51.2 3.51.2
```

`aiosqlite.Row` exists and **is literally `sqlite3.Row`** -- it is a re-export,
not a subclass. So `isinstance(row, sqlite3.Row)` and mapping-style access
`row['col']` both work, and there is no aiosqlite-specific row behaviour to
learn.

## 11. lxml

```
$ python -c "
import lxml, lxml.html, lxml.etree, importlib.metadata as md
print(lxml.__version__, md.version('lxml'))
print(lxml.etree.__version__, lxml.etree.LXML_VERSION)
print(lxml.etree.LIBXML_VERSION, lxml.etree.LIBXML_COMPILED_VERSION)
print(lxml.etree.tostring(lxml.html.fromstring('<p>a<i>b</i></p>')).decode())"
6.1.3 6.1.3
6.1.3 (6, 1, 3, 0)
(2, 14, 6) (2, 14, 6)
<p>a<i>b</i></p>
```

Both `lxml.html` and `lxml.etree` import. Runtime and compiled libxml2 agree
at 2.14.6, so no version-skew surprises. Note `lxml>=5.2` in pyproject.toml
resolved all the way to 6.1.3 -- a full major above what planning assumed.

## 12. FTS5 through aiosqlite specifically

Not just through `sqlite3`. Everything below ran on an `aiosqlite.connect()`
connection inside `asyncio.run`:

```
sqlite_version() via aiosqlite: 3.51.2
ENABLE_FTS* compile options via aiosqlite: ['ENABLE_FTS3', 'ENABLE_FTS3_PARENTHESIS', 'ENABLE_FTS5']
```

```sql
create virtual table quotes using fts5(body, loc, tokenize='porter unicode61')
```

created without error, three rows inserted, then:

```
--- plain MATCH (order by rank) ---
   {'loc': 'ch42:p7', 'body': 'the whale was a mysterious creature of the deep ocean'}

--- bm25() ---
  loc=ch42:p7 bm25=-1.0330734353925877

--- bm25() with column weights: bm25(quotes, 10.0, 1.0) ---
   [{'loc': 'ch42:p7', 'score': -1.0055914334837157}]

--- snippet(quotes, 0, '[', ']', '...', 8) ---
  loc=ch42:p7 snip=the [whale] was a mysterious creature of the...

--- highlight(quotes, 0, '<b>', '</b>') ---
   the <b>whale</b> was a mysterious creature of the deep ocean

--- porter stemming: query "whales" matches stored "whale" ---
  rows for "whales": 1

--- row_factory = aiosqlite.Row ---
  type: <class 'sqlite3.Row'> keys: ['loc', 'body'] r["loc"]: ch1:p1
```

CONFIRMED: FTS5 works through aiosqlite, and **`bm25()`, `snippet()` and
`highlight()` are all available**. `order by rank` works as an alias for the
default bm25 ordering.

One thing to design around: **`bm25()` returns a NEGATIVE score, and more
negative means more relevant** (-1.033 above). `order by bm25(t)` ascending is
therefore best-first. Do not sort descending and do not treat the value as a
0..1 confidence.

`tokenize='porter unicode61'` stems, so a query for "whales" hits stored
"whale". That is desirable for quote search but it means an exact-phrase
feature cannot rely on the FTS index alone.

## 13. pytest-asyncio in asyncio_mode=auto

pyproject.toml sets `asyncio_mode = "auto"` under `[tool.pytest.ini_options]`.
It is honoured, and a bare undecorated `async def test_x()` runs:

```
$ python -m pytest <probe>.py -v -p no:cacheprovider
platform darwin -- Python 3.13.11, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/granteds/marginalia
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 3 items

test_bare_async_no_decorator PASSED         [ 33%]
test_bare_async_uses_aiosqlite_fts5 PASSED  [ 66%]
test_sync_still_works PASSED                [100%]
============================== 3 passed in 0.03s ===============================
```

The probe file had no `@pytest.mark.asyncio` anywhere, and one of its tests did
real `aiosqlite` FTS5 work with `snippet()` and `bm25()` inside the event loop.

The config is genuinely load-bearing. Forcing strict mode breaks exactly those
two tests, which is the failure a downstream agent would see if the
`asyncio_mode` line were ever dropped:

```
$ python -m pytest <probe>.py -v -p no:cacheprovider -o asyncio_mode=strict
test_bare_async_no_decorator FAILED
test_bare_async_uses_aiosqlite_fts5 FAILED
test_sync_still_works PASSED
async def functions are not natively supported.
========================= 2 failed, 1 passed in 0.02s ==========================
```

CORRECTION: pyproject.toml asks for `pytest>=8.0` and `pytest-asyncio>=0.24`,
but what actually resolved is **pytest 9.1.1 and pytest-asyncio 1.4.0**, i.e. a
major version above planning's floor on both. Things to know about that pair:

- pytest-asyncio 1.x removed the old `event_loop` fixture. Do not write or
  request an `event_loop` fixture; use `asyncio_default_fixture_loop_scope` /
  `asyncio_default_test_loop_scope` if a loop scope needs changing.
- The run above reports `asyncio_default_fixture_loop_scope=None` and emitted
  no deprecation warning at that setting on 1.4.0.
- Sync tests are unaffected by auto mode.

The probe file was deleted after the run. `-p no:cacheprovider` was used so no
`.pytest_cache` was left behind.

## 14. Target install -- every pin has a wheel

SCOPE CORRECTION 2026-09-04: this section originally covered a Raspberry Pi
(linux aarch64). **The deployment target is an unRAID box, which is x86_64.**
The aarch64 evidence below is kept because it is real and still true, but
§14a is the one that matters for this deployment.

### 14a. linux x86_64 (unRAID) -- BUILT AND MEASURED, not just resolved

Superseding the dry-run method below: the container was actually built for
`linux/amd64` under emulation and every claim here comes from that build.

```
$ docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
    -f deploy/Dockerfile .
```

- **54.7 MB compressed / 156 MB on-disk.** Cold build 1m39s, no-cache rerun
  53s, warm 7s.
- **Nothing compiled from source.** All 14 runtime pins resolved to prebuilt
  manylinux x86_64 wheels; no `Building wheel` line appeared anywhere.
  `lxml-6.1.3-cp313-cp313-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl`.
- The build-time self-check printed
  `self-check OK: tz Jan-6/Jul-5, discord.py 2.7.1`.
- **CORRECTION, measured:** `python:3.13-slim` ALREADY ships tzdata
  (`already the newest version (2026b-0+deb13u1)`, 0 newly installed). Earlier
  notes in this project claimed slim images lack usable tzdata and that
  `ZoneInfo` would raise at runtime. For this base image that is FALSE. The
  `apt-get install tzdata` line is kept as a declared dependency, and the
  self-check is the real guard if the base image ever slims it out.
- Native arm64 for comparison: 21.7s, 55.1 MB compressed / 181 MB on-disk.
  Trap: `docker images` mis-reports a cross-loaded amd64 image (57.4MB vs
  260MB) because `--load` into a non-containerd store drops per-layer size
  metadata. Measure both the same way or the numbers are nonsense.

`requirements.txt` is runtime-only (14 pins). Dev tooling lives in
`requirements-dev.txt` so ~30MB of pytest/ruff never enters the image.

### 14b. linux aarch64 -- resolved by dry run, never built

Kept for the record, and as the worked example of the pip `--platform` trap.
Resolved without touching a Pi, using pip's platform override, because a Pi has
no compiler toolchain worth depending on and `lxml` from source there is a
long, failure-prone build.

```
$ pip install --dry-run --only-binary=:all: --python-version 3.13 \
    --platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64 \
    --platform manylinux_2_28_aarch64 --platform linux_aarch64 \
    --target <tmp> --report <tmp>.json -r requirements.txt
Would install Pygments-2.21.0 aiohappyeyeballs-2.7.1 aiohttp-3.14.3 aiosignal-1.4.0
aiosqlite-0.22.1 attrs-26.1.0 audioop-lts-0.2.2 discord.py-2.7.1 frozenlist-1.8.0
idna-3.19 iniconfig-2.3.0 lxml-6.1.3 multidict-6.7.1 packaging-26.3 pluggy-1.6.0
propcache-0.5.2 pytest-9.1.1 pytest-asyncio-1.4.0 python-dotenv-1.2.3 ruff-0.16.6
yarl-1.24.5
```

All 21 pins resolve to a prebuilt wheel, no sdists (`--only-binary=:all:`
would have failed otherwise). The interesting ones:

```
audioop-lts   0.2.2   audioop_lts-0.2.2-cp313-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl
lxml          6.1.3   lxml-6.1.3-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
aiohttp       3.14.3  aiohttp-3.14.3-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl
ruff          0.16.6  ruff-0.16.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
discord.py    2.7.1   discord_py-2.7.1-py3-none-any.whl
aiosqlite     0.22.1  aiosqlite-0.22.1-py3-none-any.whl
python-dotenv 1.2.3   python_dotenv-1.2.3-py3-none-any.whl
```

Trap for whoever automates this check: pip's `--platform` is an exact tag
match and does not imply lower manylinux versions. Asking only for
`manylinux_2_36_aarch64` fails even though the wheels are installable there:

```
$ pip install --dry-run --only-binary=:all: --python-version 3.13 \
    --platform manylinux_2_36_aarch64 ... audioop-lts ...
ERROR: Could not find a version that satisfies the requirement audioop-lts (from versions: none)
```

That was a flag mistake, not a missing wheel. Pass several `--platform` tags.
`manylinux_2_17` needs glibc >= 2.17; Raspberry Pi OS Bookworm ships 2.36, so
all of the above are compatible.

The same trap bit again on x86_64: probing only `manylinux_2_17_x86_64` reports
`audioop-lts` as unavailable, because it publishes
`manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64` and pip matches
tags exactly. It installs fine. If you automate a wheel check, pass the full
tag set or you will chase a wheel that is already there.

Python version is not a concern on the unRAID path: the container brings its own
3.13 from `python:3.13-slim`, so the host's Python is irrelevant. That was the
Pi path's hardest unsolved problem (Bookworm ships 3.11 and none of these cp313
wheels install under it) and containerising removed it entirely.

## 15. zoneinfo

```
$ python -c "
import datetime
from zoneinfo import ZoneInfo, available_timezones, TZPATH
d = datetime.datetime(2026,1,15,12,0, tzinfo=ZoneInfo('America/Chicago'))
s = datetime.datetime(2026,7,15,12,0, tzinfo=ZoneInfo('America/Chicago'))
print(d.utcoffset(), d.tzname(), '|', s.utcoffset(), s.tzname())
print(len(available_timezones()))
print(TZPATH)"
-1 day, 18:00:00 CST | -1 day, 19:00:00 CDT
598
('/usr/share/zoneinfo', '/usr/lib/zoneinfo', '/usr/share/lib/zoneinfo', '/etc/zoneinfo')
```

Real IANA data, and DST is handled: -6h/CST in January, -5h/CDT in July.
598 zones available.

```
$ python -c "
import importlib.metadata as md
try: print(md.version('tzdata'))
except md.PackageNotFoundError: print('tzdata: NOT INSTALLED')"
tzdata: NOT INSTALLED
```

**There is no `tzdata` pip package in this environment.** zoneinfo reads the
operating system's tzdb from `/usr/share/zoneinfo`, so on THIS Mac timezone
correctness depends on the host being patched.

RESOLVED 2026-09-04 for the deployment target, and the worry above turned out
to be misplaced. In the container the tzdb comes from the IMAGE, not the host,
and the built image was measured: `python:3.13-slim` already ships
`tzdata 2026b-0+deb13u1` (0 newly installed when apt is asked for it). So no
pip `tzdata` is needed on the unRAID path either.

Do not delete the `apt-get install tzdata` line in `deploy/Dockerfile` on the
strength of that measurement. It is a DECLARED dependency: the base image is
free to slim it out in a future tag, and the build-time self-check in that
Dockerfile -- which asserts `America/Chicago` reports different offsets in
January and July -- is what turns that into a failed build instead of reminders
firing an hour wrong for half the year with nothing in the logs.

## 16. Other tool versions

```
$ pip freeze
aiohappyeyeballs==2.7.1   attrs==26.1.0        lxml==6.1.3          pytest==9.1.1
aiohttp==3.14.3           audioop-lts==0.2.2   multidict==6.7.1     pytest-asyncio==1.4.0
aiosignal==1.4.0          discord.py==2.7.1    packaging==26.3      python-dotenv==1.2.3
aiosqlite==0.22.1         frozenlist==1.8.0    pluggy==1.6.0        ruff==0.16.6
                          idna==3.19           propcache==0.5.2     yarl==1.24.5
                          iniconfig==2.3.0     Pygments==2.21.0
```

(That is the same content as `requirements.txt`, reflowed to fit. The file
itself is one pin per line, straight from `pip freeze`.)

```
$ .venv/bin/ruff --version
ruff 0.16.6
```

ruff reads the project config correctly -- `ruff check --show-settings`
reports:

```
linter.unresolved_target_version = 3.13
linter.line_length = 100
linter.pycodestyle.max_line_length = 100
formatter.unresolved_target_version = 3.13
analyze.target_version = 3.13
cache_dir = "/Users/granteds/marginalia/.ruff_cache"
```

so `target-version = "py313"` and `line-length = 100` from pyproject.toml are
both in force, and the `select = ["E","F","I","UP","B","ASYNC"]` rule set
loaded without complaint on 0.16.6. CORRECTION: pyproject asks for
`ruff>=0.6`; what resolved is 0.16.6.

python-dotenv 1.2.3 API, for whoever loads config:

```
load_dotenv(dotenv_path=None, stream=None, verbose=False, override=False, interpolate=True, encoding='utf-8') -> bool
dotenv_values(dotenv_path=None, stream=None, verbose=False, interpolate=True, encoding='utf-8') -> Dict[str, Optional[str]]
find_dotenv(filename='.env', raise_error_if_not_found=False, usecwd=False) -> str
```

Note `override=False` by default, so a real environment variable wins over
`.env`. `dotenv.__version__` does not exist; use
`importlib.metadata.version('python-dotenv')`.

## 17. Side effects of producing this file

- `/Users/granteds/marginalia/.venv/` created and populated.
- `/Users/granteds/marginalia/.ruff_cache/` created by the ruff invocations in
  section 16. It is already covered by `.gitignore`.
- No `.pytest_cache` (all pytest runs used `-p no:cacheprovider`).
- The pytest probe file and the pip dry-run report were written inside
  `.venv/` and deleted afterwards. `.venv/` now contains only
  `bin include lib pyvenv.cfg`.
- No Discord token, API key or other credential was written anywhere, and
  nothing here needs a live Discord connection to re-verify.
