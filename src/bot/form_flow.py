"""Discord UI for Delilah's interactive, LLM-generated questionnaire forms.

This is the presentation layer that pairs with the state/logic in
``src/services/form_flow.py``. It renders a "Start Form" button, builds a
``discord.ui.Modal`` dynamically per form page (one ``TextInput`` per
question), and chains pages together via a "Continue" button — necessary
because ``discord.ui.InteractionFollowup`` has no ``send_modal`` in
discord.py 2.7.1, so multi-page modals must advance page-by-page.

Interaction model:
    Start button -> Modal (page 1) -> ephemeral "Continue" button
                 -> Modal (page 2) -> ... -> last Modal submit -> finalize
"""

from __future__ import annotations

import discord

from src.services.form_flow import (
    FormSession,
    get_session,
    record_answers,
    finalize_form as svc_finalize_form,
)

# discord.TextStyle mapping for the supported input_type set. Anything we don't
# recognize defaults to a single-line short input.
TEXT_STYLE = {
    "short": discord.TextStyle.short,
    "long": discord.TextStyle.paragraph,
    "number": discord.TextStyle.short,
    "email": discord.TextStyle.short,
    "paragraph": discord.TextStyle.paragraph,
}


class StartFormView(discord.ui.View):
    """Posts a single button that opens the first page of a queued form."""

    def __init__(self, session_id: str, owner_uid: int):
        super().__init__(timeout=600)
        self.session_id = session_id
        self.owner_uid = int(owner_uid)

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_uid

    @discord.ui.button(label="Start Form", style=discord.ButtonStyle.primary, emoji="📋")
    async def _start(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(
            f" [FORM] StartFormView._start interaction={interaction.id} "
            f"user={interaction.user.id} channel={interaction.channel_id} "
            f"session={self.session_id} owner={self.owner_uid}",
            flush=True,
        )
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "Only the person who requested this form can fill it out.", ephemeral=True
            )
            return
        try:
            sess = get_session(self.session_id)
            if sess is None:
                await interaction.response.send_message(
                    "This form session is no longer available. "
                    "Ask Delilah to create a new one.",
                    ephemeral=True,
                )
                return
            # Resume, don't reset: open the first page with any unanswered
            # question. This keeps a form alive if the user misses the
            # ephemeral "Continue" button and clicks Start again — a
            # half-filled form carries on instead of silently restarting.
            resume_index = 0
            for idx, page in enumerate(sess.pages):
                if any(q["key"] not in sess.answers for q in page["questions"]):
                    resume_index = idx
                    break
            print(
                f" [FORM] StartFormView opening page {resume_index + 1}"
                f" of {len(sess.pages)} for session={self.session_id}",
                flush=True,
            )
            modal = FormModal(self.session_id, resume_index, self.owner_uid)
            await interaction.response.send_modal(modal)
        except Exception as exc:
            print(f" [FORM] _start send_modal failed: {exc}")
            try:
                await interaction.response.send_message(
                    "Couldn't open the form (discord rejected the modal). "
                    "Try asking Delilah for the form again.",
                    ephemeral=True,
                )
            except Exception:
                pass


class FormModal(discord.ui.Modal):
    """A single form page rendered as a Modal with one TextInput per question.

    Modals can't carry extra constructor kwargs through the decorator, so the
    page index and session id are stored on the instance and used to look up
    the owning ``FormSession`` in ``on_submit``.
    """

    def __init__(self, session_id: str, page_index: int, owner_uid: int):
        sess = get_session(session_id)
        page = sess.pages[page_index] if sess else {"questions": []}
        super().__init__(title=f"Page {page_index + 1} of {len(sess.pages) if sess else 1}")
        self.session_id = session_id
        self.page_index = page_index
        self.owner_uid = int(owner_uid)

        for q in page["questions"]:
            # Discord hard limits: label <= 45 chars, placeholder <= 100 chars.
            # The schema service is generous (200/4000) because the LLM emits
            # free text; the trim happens here at the platform boundary. An
            # over-long label/placeholder makes Discord reject the whole modal
            # with HTTP 400, so the form would appear completely dead.
            label = q["label"]
            if len(label) > 45:
                label = label[:42] + "…"
            placeholder = q.get("help_text") or q["label"] or "Your answer"
            if len(placeholder) > 100:
                placeholder = placeholder[:97] + "…"
            self.add_item(
                discord.ui.TextInput(
                    label=label,
                    placeholder=placeholder,
                    custom_id=q["key"][:100],
                    style=TEXT_STYLE.get(q["input_type"], discord.TextStyle.short),
                    required=q["required"],
                    max_length=4000 if q["input_type"] == "long" else 1000,
                )
            )

    def _find_input(self, key: str) -> discord.ui.TextInput | None:
        for child in self.children:
            if isinstance(child, discord.ui.TextInput) and getattr(child, "custom_id", None) == key:
                return child
        return None

    async def on_submit(self, interaction: discord.Interaction):
        print(
            f" [FORM] on_submit interaction={interaction.id} "
            f"user={interaction.user.id} channel={interaction.channel_id} "
            f"session={self.session_id} page={self.page_index}",
            flush=True,
        )
        if interaction.user.id != self.owner_uid:
            print(
                f" [FORM] on_submit rejected: user={interaction.user.id} "
                f"owner={self.owner_uid}",
                flush=True,
            )
            await interaction.response.send_message(
                "Only the form owner can submit this form.", ephemeral=True
            )
            return

        sess = get_session(self.session_id)
        if sess is None:
            print(
                f" [FORM] on_submit rejected: session {self.session_id} "
                f"not found",
                flush=True,
            )
            await interaction.response.send_message(
                "This form session is no longer available.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        print(
            f" [FORM] on_submit deferred interaction={interaction.id} "
            f"session={self.session_id} page={self.page_index}",
            flush=True,
        )

        page = sess.pages[self.page_index]
        answers = {}
        for q in page["questions"]:
            text_input = self._find_input(q["key"])
            answers[q["key"]] = text_input.value if text_input else ""
        print(
            f" [FORM] answers recorded session={self.session_id} "
            f"answers={list(answers.keys())}",
            flush=True,
        )
        record_answers(self.session_id, answers)

        total = len(sess.pages)
        if self.page_index < total - 1:
            view = PageAdvanceView(self.session_id, self.page_index + 1, self.owner_uid)
            await interaction.followup.send(
                "✅ Answers recorded. Click below to continue.", view=view, ephemeral=True
            )
        else:
            summary = await svc_finalize_form(self.session_id)
            await interaction.followup.send(
                f"✅ Form **{summary['form_title']}** complete — "
                f"{len(summary['claims'])} answer(s) saved to your world model.",
                ephemeral=True,
            )


class PageAdvanceView(discord.ui.View):
    """Ephemeral follow-up that opens the next page Modal on a button click."""

    def __init__(self, session_id: str, page_index: int, owner_uid: int):
        super().__init__(timeout=600)
        self.session_id = session_id
        self.page_index = page_index
        self.owner_uid = int(owner_uid)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, emoji="▶️")
    async def _continue(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(
            f" [FORM] PageAdvanceView._continue interaction={interaction.id} "
            f"user={interaction.user.id} channel={interaction.channel_id} "
            f"session={self.session_id} page={self.page_index}",
            flush=True,
        )
        if interaction.user.id != self.owner_uid:
            await interaction.response.send_message(
                "Only the form owner can continue.", ephemeral=True
            )
            return
        try:
            modal = FormModal(self.session_id, self.page_index, self.owner_uid)
            await interaction.response.send_modal(modal)
        except Exception as exc:
            print(f" [FORM] _continue send_modal failed: {exc}")
            try:
                await interaction.response.send_message(
                    "Couldn't open the next page (discord rejected the modal). "
                    "Try asking Delilah for the form again.",
                    ephemeral=True,
                )
            except Exception:
                pass

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="✕")
    async def _close(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(
            f" [FORM] PageAdvanceView._close interaction={interaction.id} "
            f"user={interaction.user.id} channel={interaction.channel_id}",
            flush=True,
        )
        await interaction.response.edit_message(view=None)


async def render_start_button(
    reply_msg: discord.Message,
    session_id: str,
    owner_uid: int,
    form_title: str,
) -> discord.ui.View | None:
    """Post the "Start Form" button into the advisor's reply channel.

    Returns the view so the caller can defer-stop it if the turn ends early;
    returns None on failure (logged) so the advisor loop never hard-fails
    because of a UI rendering hiccup.
    """
    try:
        view = StartFormView(session_id, owner_uid)
        await reply_msg.channel.send(
            content=(
                f"📋 **{form_title}** — Delilah prepared a form for you. "
                f"Click below to fill it out:"
            ),
            view=view,
        )
        return view
    except Exception as exc:  # noqa: BLE001 — UI rendering must never kill the advisor
        print(f" [FORMS] render_start_button failed: {exc}")
        return None


__all__ = [
    "StartFormView",
    "FormModal",
    "PageAdvanceView",
    "render_start_button",
]
