"""Bounded interaction responses: never retry an expired acknowledgement."""
import logging

import discord

LOGGER = logging.getLogger(__name__)


async def acknowledge(interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        return True
    except (discord.HTTPException, discord.InteractionResponded, OSError) as error:
        LOGGER.warning(
            "Interaction acknowledgement failed; no job started. type=%s code=%s",
            type(error).__name__, getattr(error, "code", None),
        )
        return False


async def reply(interaction, content, **kwargs):
    try:
        if interaction.is_expired():
            LOGGER.warning("Interaction expired; response omitted. Check saved job reports.")
            return False
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=True, **kwargs)
        return True
    except (discord.HTTPException, discord.InteractionResponded, OSError) as error:
        LOGGER.warning(
            "Interaction response failed; not sending another error response. type=%s code=%s",
            type(error).__name__, getattr(error, "code", None),
        )
        return False
