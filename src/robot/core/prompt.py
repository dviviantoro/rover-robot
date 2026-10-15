"""System prompt and conversation history.

system-overview.md step 5: "assemble: persona + language instruction + retrieved
context + recent history + new turn". Retrieval is M7, so it is absent here and the
seam for it is marked.

memory-budget.md rule 2 -- "cap context and summarize old turns; unbounded history is
a silent memory leak" -- is why History is bounded by turn count. Rolling
summarisation is not implemented yet; the cap is the part that stops the leak.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from robot.core import reply as reply_mod
from robot.core.language import LANGUAGES

# The standing line from system-overview.md § Language routing. Together with the
# per-turn instruction this is the entire multilingual logic -- no mode switching.
STANDING_LANGUAGE_RULE = (
    "Always reply in the same language the user just spoke, entirely in that one "
    "language. Never mix in Chinese, Japanese or Korean characters or words."
)

DEFAULT_PERSONA = (
    "You are a small friendly robot. You are speaking out loud, so keep replies short "
    "and conversational -- one or two sentences unless asked for more. Never use "
    "markdown, bullet points, emoji, or code blocks: everything you write is read "
    "aloud by a speech synthesiser."
)

# Asking for the tag is what makes language routing work at all -- see
# robot.core.language for why the ASR cannot provide it.
LANGUAGE_DECLARATION_RULE = """Every reply has exactly this shape:

  [lang:xx] then the words you say out loud.

The tag is required and always comes first. Use exactly [lang:en] for English,
[lang:id] for Bahasa Indonesia, or [lang:ar] for Arabic -- those three codes and no
others. Never mention the tag or explain it."""

# The physical actions. Written as inline markers rather than an API call so the robot
# moves WHILE it is speaking rather than after -- see robot.core.tools.
TOOL_RULE = """You have a body and can act while you speak. To act, write a marker
inline in your reply. The markers are removed before your words are spoken aloud, so
write the sentence as if the action is simply happening.

  [do:drive vx=<forward m/s> vy=<right m/s> omega=<turn rad/s> ms=<duration>]
  [do:stop]
  [do:face <happy|thinking|listening|speaking|sad|neutral>]
  [do:gesture <wave|point|home|nod>]
  [do:remember <a fact worth keeping>]

Guidance on values: a small move is vx=0.2 for ms=1000; a normal one vx=0.35 for
ms=1500. Turning: omega=1.0 is a gentle turn, omega=2.0 a brisk one. Strafing sideways
uses vy. Limits are vx and vy within 1.0, omega within 3.0, ms within 10000; anything
larger is clamped, so ask for what you mean.

ALWAYS SPEAK. A marker is not a reply -- it is silent, and a robot that moves without
a word seems broken. Every reply must contain words for the person to hear, even when
you are acting. Put the marker inside or beside that sentence:

  [lang:en] Sure, moving forward now. [do:drive vx=0.2 ms=1000]
  [lang:id] Baik, saya maju sedikit. [do:drive vx=0.2 ms=1000]

Only move when the person actually asked you to move. Never narrate a marker, never
explain that you are using a tool, and never write a marker inside quotes."""

# Hardware told us something the model should know before it speaks again.
HARDWARE_NOTE_PREFIX = (
    "Note from your body since your last reply -- take it into account, and say so "
    "plainly if it means you did not do what you said you would: "
)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str


class History:
    """Recent turns, bounded. Oldest fall off the back."""

    def __init__(self, max_turns: int = 12) -> None:
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add_user(self, text: str) -> None:
        # Two user turns in a row is not a conversation, and some chat templates refuse
        # to render one at all -- see `alternate()`. It happens whenever a turn produces
        # no reply: the question is recorded, nothing answers it, and the next question
        # lands straight after it. Fold the unanswered one in rather than storing a
        # shape the model can never be shown.
        if self._turns and self._turns[-1].role == "user":
            previous = self._turns.pop()
            text = f"{previous.content}\n{text}"
        self._turns.append(Turn("user", text))

    def add_assistant(self, text: str) -> None:
        self._turns.append(Turn("assistant", text))

    def drop_last_assistant(self) -> None:
        """After a barge-in, the assistant only said part of its reply.

        Keeping the full generated text would make the model believe it said things
        the user never heard.
        """
        if self._turns and self._turns[-1].role == "assistant":
            self._turns.pop()

    def messages(self) -> list[dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in self._turns]

    def clear(self) -> None:
        self._turns.clear()

    def __len__(self) -> int:
        return len(self._turns)


# Characters per token, deliberately pessimistic. There is no tokeniser in this process
# and adding one costs a dependency for an estimate; Latin text runs about 4 chars per
# token and Arabic noticeably fewer, so 3 is the number that does not under-count the
# language most likely to overflow.
CHARS_PER_TOKEN = 3


def trim_context(chunks: list[str], max_chars: int) -> list[str]:
    """Keep the best-ranked retrieved notes that fit, dropping from the worst.

    Retrieval returns k chunks of up to CHUNK_CHARS each -- 4 x 1200 characters is
    roughly 1600 tokens, which on its own does not fit a 2048-token context beside a
    persona, tool rules and any history at all. The request then comes back
    `exceeds the available context size` and the robot speaks its unavailable line, so
    a busy knowledge store looks exactly like a broken LLM.

    Chunks arrive best-first, so dropping from the end drops the least relevant.
    """
    kept: list[str] = []
    used = 0
    for chunk in chunks:
        if used + len(chunk) > max_chars:
            break
        kept.append(chunk)
        used += len(chunk)
    return kept


def trim_history(messages: list[dict[str, str]], max_chars: int) -> list[dict[str, str]]:
    """Drop the oldest exchanges until the whole prompt fits.

    The system message and the turn being answered are never dropped -- without them
    there is no question to answer and no instructions for answering it. If those two
    alone exceed the budget the caller has a configuration problem that trimming cannot
    fix, and it is left to fail loudly rather than silently sending half a persona.
    """
    def size(rows: list[dict[str, str]]) -> int:
        return sum(len(r["content"]) for r in rows)

    if size(messages) <= max_chars or len(messages) <= 2:
        return messages
    head = messages[:1] if messages and messages[0]["role"] == "system" else []
    tail = messages[-1:]
    middle = messages[len(head):-1]
    while middle and size(head + middle + tail) > max_chars:
        middle = middle[1:]
    return head + middle + tail


def language_line(tag: str, source: str | None = None) -> str:
    """The per-turn language instruction, worded by how sure we are of `tag`.

    The wording used to be one sentence -- "The previous turn was in X. If this one is
    too: reply in X" -- because the hint WAS the previous turn's language. It is now
    mostly the language the person is speaking this very turn, worked out from their
    words, and describing that as "the previous turn" is untrue and weak: a small model
    given Indonesian notes, Indonesian history and a polite maybe answered an English
    question in Indonesian, which is the bug this exists to fix.

      text, script   read off the transcript itself. Said as a fact and as an order,
                     and it names the two things that pull a reply the wrong way --
                     the notes and the model's own earlier replies.
      asr            only the recogniser's id, which is wrong on a sentence built
                     around a name. An order would be wrong whenever it is, so this one
                     lets the words overrule it.
      anything else  the previous turn's language, offered as a default, exactly as before.
    """
    lang = LANGUAGES[tag]
    if source in ("text", "script"):
        return (f"The person just spoke {lang.name}. {lang.instruction} That holds even "
                f"if any notes or your earlier replies were in another language.")
    if source == "asr":
        return (f"The speech recogniser heard {lang.name}. {lang.instruction} If the "
                f"person's words are plainly in another language, follow their words.")
    return (f"The previous turn was in {lang.name}. "
            f"If this one is too: {lang.instruction} "
            f"If the user has switched language, follow the user.")


def build_system_prompt(
    persona: str = DEFAULT_PERSONA,
    hint: str | None = None,
    tools: bool = True,
    context: list[str] | None = None,
    reply_format: str = "markers",
    hint_source: str | None = None,
) -> str:
    """Persona + the standing language rule + the declaration instruction.

    `hint` carries the per-turn routing instruction from the table in
    system-overview.md ("Reply in English", ...). `hint_source` says where it came from
    (see `language.heard`) and therefore how firmly it is worded -- see `language_line`.
    With no source it is the previous turn's language and a hint, not an order: the
    standing rule above it wins if the user switched language.
    """
    parts = [persona]

    # Retrieved knowledge goes BEFORE the language rules, not after.
    #
    # Measured in M7: with an English note in the prompt, an Arabic question came back
    # in Indonesian. The notes are usually in whichever language the document was
    # written in, and a block of English sitting closest to the user's turn drags the
    # reply towards it. Putting the rules last, and saying explicitly that the notes'
    # language means nothing, restores the M4 behaviour.
    if context:
        joined = "\n\n".join(f"[{i + 1}] {chunk}" for i, chunk in enumerate(context))
        parts.append(
            "Here is what you know that may be relevant. Use it if it answers the "
            "question, and ignore it if it does not. Do not mention these notes or "
            "their numbering.\n\n" + joined + "\n\n"
            "These notes may be written in a different language from the conversation. "
            "That means nothing: they are reference material, not an example of how to "
            "reply. Answer in the language the PERSON used, never the language of the "
            "notes."
        )

    parts.append(STANDING_LANGUAGE_RULE)
    if reply_format == "json":
        # The shape is enforced by a grammar, so this is not asking for JSON -- it is
        # explaining what each field MEANS. Without it the model fills them in, validly
        # and at random: a face on every turn, and a foot_direction whenever movement is
        # mentioned rather than requested.
        parts.append(reply_mod.INSTRUCTIONS)
    elif tools:
        parts.append(TOOL_RULE)
    if hint and hint in LANGUAGES:
        parts.append(language_line(hint, hint_source))
    if reply_format == "json":
        # No declaration rule: `language` is a field of the object, constrained to the
        # three the robot speaks. Asking for `[lang:xx]` as well would put a tag inside
        # the text that gets spoken.
        return "\n\n".join(parts)

    # The declaration rule goes LAST, after the hint. Measured in M7: buried in the
    # middle of a prompt that also carries retrieved notes and the tool rules, the model
    # dropped the [lang:xx] tag on turns where it was calling a tool -- the reply text
    # was in the right language but nothing downstream knew, so it got the wrong voice.
    # Recency is doing real work here.
    parts.append(LANGUAGE_DECLARATION_RULE)
    return "\n\n".join(parts)


def alternate(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge consecutive same-role messages so user and assistant strictly alternate.

    Gemma-3's chat template raises `Conversation roles must alternate
    user/assistant/user/assistant/...` and llama.cpp turns that into HTTP 400 -- the
    orchestrator then speaks its canned unavailable line, which looks exactly like the
    LLM being down. Qwen's template tolerates the same input, which is why this only
    appeared when the dashboard's Memory page was used to switch models.

    Two shapes trigger it, and the robot produces both of them itself.

    **Consecutive same-role turns.** A turn that gets no reply records the question and
    nothing else, so the next question follows a user turn.

    **A history that begins with an assistant turn.** `History` is a bounded deque, and
    it drops from the left. Its entries alternate, so evicting an even number leaves a
    user turn in front and everything is fine -- but one unanswered turn makes the count
    odd, the next eviction takes a `user`, and the window now opens on the assistant's
    reply to a question that has been forgotten. Gemma's template strips a leading
    system message and then requires the rest to start with `user`, so it raises. That
    orphaned reply is a fragment of a conversation whose beginning is already gone;
    dropping it costs nothing and is the only thing that can be done with it.

    Both are silent under Qwen's more tolerant template. Once either has happened every
    later request is rejected too, so the robot stays stuck on the canned unavailable
    line until robot-core is restarted. Enforcing the invariant here means no caller has
    to remember it.
    """
    merged: list[dict[str, str]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"] != "system":
            merged[-1] = {"role": message["role"],
                          "content": f"{merged[-1]['content']}\n{message['content']}"}
        else:
            merged.append(dict(message))

    lead = 1 if merged and merged[0]["role"] == "system" else 0
    while len(merged) > lead and merged[lead]["role"] == "assistant":
        merged.pop(lead)
    return merged


def build_messages(
    system: str,
    history: History,
    user_text: str,
    max_chars: int | None = None,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system}, *history.messages(),
                {"role": "user", "content": user_text}]
    if max_chars:
        messages = trim_history(messages, max_chars)
    # Alternation is enforced AFTER trimming: dropping an exchange can leave the window
    # opening on an assistant turn, which is the shape Gemma's template refuses.
    return alternate(messages)
