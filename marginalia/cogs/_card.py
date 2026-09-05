"""Keeping the monthly signup card current after it has been posted.

The card is the most-seen message the bot sends and it lives all month, so it must not
freeze at the state it had when /cycle-open ran. Two different jobs, deliberately:

``sync_count`` runs on every join and leave, so it patches the ONE count field and
uploads nothing. ``rebuild`` runs when /cycle-book changes which book the month is
reading, and re-sends the whole embed because a cover cannot be patched onto a card that
was posted without one.

Every failure here is swallowed. A stale card is cosmetic; failing someone's /join over
it is not.
"""

from __future__ import annotations

import discord

from marginalia import library
from marginalia.cogs._views import JOINED_FIELD, cover_file, join_embed, roster_embed
from marginalia.cycle import roster_ids
from marginalia.db import Database

# How far back to hunt for a signup card whose id was never recorded. Generous enough
# for a busy month, bounded so a deleted card cannot cost a full history walk per join.
HISTORY_SCAN = 200


async def _fetch(interaction: discord.Interaction, cohort, column: str) -> discord.Message | None:
    """One of the cohort's remembered messages, by the column holding its id."""
    mid = cohort[column]
    if not mid:
        return None
    channel = interaction.guild.get_channel(int(cohort["channel_id"]))
    return await channel.fetch_message(int(mid)) if channel else None


async def _search(db: Database, interaction: discord.Interaction, cohort):
    """Find the signup card by its own Join button, for a cohort whose id was never
    stored -- one opened before migration 3, or a restored backup. The id is written
    back, so this scan happens at most once per cohort rather than on every join."""
    channel = interaction.guild.get_channel(int(cohort["channel_id"]))
    me = interaction.client.user
    if channel is None or me is None:
        return None
    want = f"mgl:join:{cohort['id']}"
    async for msg in channel.history(limit=HISTORY_SCAN):
        if msg.author.id == me.id and any(
                getattr(child, "custom_id", "") == want
                for row in msg.components for child in getattr(row, "children", ())):
            await db.run("UPDATE cohorts SET signup_message_id = ? WHERE id = ?",
                         msg.id, cohort["id"])
            return msg
    return None


async def _message(db: Database, interaction: discord.Interaction, cohort):
    """The signup card. A button interaction already carries it; a slash command fetches
    it by the stored id, and falls back to hunting for it once."""
    if interaction.message is not None:
        return interaction.message
    return (await _fetch(interaction, cohort, "signup_message_id")
            or await _search(db, interaction, cohort))


async def sync_count(db: Database, interaction: discord.Interaction, cohort) -> None:
    """Patch 'Joined so far' in place, so the thumbnail and every other field survive."""
    try:
        msg = await _message(db, interaction, cohort)
        if msg is None or not msg.embeds:
            return
        embed = msg.embeds[0]
        at = next((i for i, f in enumerate(embed.fields) if f.name == JOINED_FIELD), None)
        count = str(len(await roster_ids(db, cohort["id"])))
        if at is None or embed.fields[at].value == count:
            return  # unchanged: do not spend an edit, they are rate-limited per message
        embed.set_field_at(at, name=JOINED_FIELD, value=count, inline=True)
        await msg.edit(embed=embed)
    except (discord.HTTPException, AttributeError, KeyError):
        pass


async def sync_roster(db: Database, interaction: discord.Interaction, cohort) -> None:
    """Rewrite the newest /roster card so a public headcount is not frozen at the moment
    somebody happened to ask. Rebuilt whole rather than patched: it carries no attachment,
    so there is nothing to lose and the member list changes as well as the number."""
    try:
        msg = await _fetch(interaction, cohort, "roster_message_id")
        if msg is None:
            return
        ids = await roster_ids(db, cohort["id"])
        month = str(cohort["cycle_month"])
        role = interaction.guild.get_role(int(cohort["role_id"] or 0))
        head = f"{role.mention if role else month} -- {len(ids)} reading this month."
        if msg.content == head:
            return  # unchanged; an edit costs a request and the message's rate limit
        # An edit never re-notifies, and none() keeps that true if Discord ever changes.
        await msg.edit(content=head, embed=roster_embed(month, ids),
                       allowed_mentions=discord.AllowedMentions.none())
    except (discord.HTTPException, AttributeError, KeyError):
        pass


async def rebuild(db: Database, interaction: discord.Interaction, cohort) -> bool:
    """Re-send the card's embed from current state, cover included. True if it landed."""
    try:
        msg = await _message(db, interaction, cohort)
        if msg is None:
            return False
        row = await db.one("SELECT b.id, b.title, b.author FROM books b"
                           " JOIN cohorts c ON c.book_id = b.id WHERE c.id = ?", cohort["id"])
        if row is None:
            return False
        blob = await library.cover(db, int(row["id"]))
        file = cover_file(blob)
        embed = join_embed(str(cohort["cycle_month"]), row["title"], row["author"],
                           len(await roster_ids(db, cohort["id"])), file)
        # attachments= REPLACES the message's files, which is the only way to put a cover
        # on a card posted before the book had one. Omitting it would keep the old file
        # and leave attachment://cover.jpg pointing at nothing.
        await msg.edit(embed=embed, attachments=[file] if file else [])
        return True
    except (discord.HTTPException, AttributeError, KeyError):
        return False
