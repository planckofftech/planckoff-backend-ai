"""The loop every pricing agent runs: ask the model, run the skill it asks for, repeat.

An agent is a model given a task and a few skills. Each turn the model either
asks for skills -- search the book, open a page, look at its picture -- and is
given what they return, or hands in its answer through its final skill. This is
what the chat did that step A and step B could not: look, learn where to look
next, and look again.

A skill is code in this repo. The model never runs code of its own; it can only
ask for the skills it was given, with arguments its schema allows.

Bounds, so a lost agent ends instead of wandering:

  max_steps   calls to the model. On the last one the model is told to hand in
              what it has and may only call its final skill.
  max_images  page pictures sent. Past it, page_image says to use read_page.

An agent that runs out of steps without handing in, stops answering in plain
words, or fails, comes back with no answer and says why. Its answer is never
made up for it.

Every call is logged to call_log like step A's and B's, with the pages its
skills opened, so the cost report covers agents too.

Each call sends the whole conversation again, and it grows with every page
read: Agent 1's first run over the Schlage book sent 1.12 million prompt tokens
in 18 calls, 33 thousand of them from the cache ($4.62). So the newest message is
marked for the provider's prompt cache on every call, and the next call reads
everything before it from the cache instead of at full price.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.price_book import call_log

log = logging.getLogger(__name__)

MAX_STEPS = 20
MAX_IMAGES = 6
# Room for one reply. Models that think before answering spend their thinking
# from the same allowance (see page_finder.REPLY_TOKENS).
REPLY_TOKENS = 8000
# OpenRouter refusing for money or the key: no later call can succeed either.
_STOP_ALL = (401, 402, 403)
_CACHE = {"type": "ephemeral"}


@dataclass
class Reply:
    """What a skill gives back to the model."""

    text: str
    image: bytes | None = None
    image_label: str = ""
    pages: list[int] = field(default_factory=list)   # pdf pages it opened or searched to
    error: bool = False                               # the request was wrong; the model may retry


@dataclass
class Skill:
    name: str
    description: str
    parameters: dict[str, Any]                        # JSON schema of the arguments
    run: Callable[[dict[str, Any]], Awaitable[Reply]]
    # Handing in the answer: the loop ends when it returns without error.
    final: bool = False


@dataclass
class Turn:
    """One skill run, for the live step shown in the UI and the record of a run."""

    step: int
    skill: str
    args: dict[str, Any]
    said: str                                         # the start of what the skill returned
    pages: list[int] = field(default_factory=list)


@dataclass
class Outcome:
    step: str
    model: str
    answer: dict[str, Any] | None = None              # the final skill's arguments
    stopped: str = ""         # answered | step_cap | no_answer | cut_off | error
    note: str = ""
    turns: list[Turn] = field(default_factory=list)
    calls: int = 0
    images: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0            # of prompt_tokens, read from the provider's cache
    completion_tokens: int = 0
    usd: float = 0.0                  # from the token price list, as every step is costed
    billed_usd: float = 0.0           # what OpenRouter billed, cache discount included

    @property
    def pages(self) -> list[int]:
        return list(dict.fromkeys(p for t in self.turns for p in t.pages))


OnTurn = Callable[[Turn], Awaitable[None]]


def _mark_newest(messages: list[dict[str, Any]]) -> None:
    """Move the moving cache mark to the newest message.

    The system prompt and the task keep theirs; with this one that is three of
    the four marks Anthropic allows. Providers without a prompt cache ignore it.
    """
    for m in messages[2:]:
        if isinstance(m.get("content"), list):
            for part in m["content"]:
                part.pop("cache_control", None)
    newest = messages[-1]
    if len(messages) <= 2:
        return
    if isinstance(newest.get("content"), str):
        if not newest["content"]:
            return
        newest["content"] = [{"type": "text", "text": newest["content"]}]
    if isinstance(newest.get("content"), list) and newest["content"]:
        newest["content"][-1]["cache_control"] = dict(_CACHE)


def tool(skill: Skill) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": skill.name, "description": skill.description,
                         "parameters": skill.parameters}}


async def run(*, step: str, model: str, system: str, task: str, skills: list[Skill],
              max_steps: int = MAX_STEPS, max_images: int = MAX_IMAGES,
              reply_tokens: int = REPLY_TOKENS,
              manufacturer: str = "", books: list[str] | None = None,
              product_keys: list[str] | None = None, on_turn: OnTurn | None = None) -> Outcome:
    """Run one agent to its answer, or to the bound that stops it."""
    from app.ai import spend
    from app.ai.client import AiUnavailableError, AiUpstreamError, get_client
    from app.ai.pricing import cost_usd

    by_name = {s.name: s for s in skills}
    final = next((s for s in skills if s.final), None)
    tools = [tool(s) for s in skills]
    # The system prompt and the task are the same on every turn: marked for the
    # provider's prompt cache where it has one, ignored where it does not.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "text", "text": system, "cache_control": dict(_CACHE)}]},
        {"role": "user", "content": [{"type": "text", "text": task, "cache_control": dict(_CACHE)}]},
    ]
    out = Outcome(step, model)
    nudged = False

    for n in range(1, max_steps + 1):
        last = n == max_steps
        if last and final:
            messages.append({"role": "user", "content":
                             f"This is your last step. Hand in your answer now with {final.name}, "
                             "with what you have found, and say in it what is missing."})
        _mark_newest(messages)
        timer = call_log.Timer()
        try:
            response = await get_client().chat.completions.create(
                model=model, temperature=0, max_tokens=reply_tokens, messages=messages, tools=tools,
                # Asks OpenRouter for the cost it bills, which the price list cannot
                # know: cached prompt tokens are charged at a fraction.
                extra_body={"usage": {"include": True}},
                tool_choice={"type": "function", "function": {"name": final.name}} if last and final
                else "auto",
            )
        except AiUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - one agent's failure is its own
            if getattr(exc, "status_code", None) in _STOP_ALL:
                # Out of credit or a bad key fails every agent alike: the run
                # says so once instead of every product failing on its own.
                from app.ai.vision_extract import _upstream_reason
                raise AiUpstreamError(_upstream_reason(exc)) from exc
            log.warning("agent %s: call %d failed: %s", step, n, exc)
            call_log.add(call_log.Call(step, model, manufacturer, books or [], [], product_keys or [],
                                       0, 0, 0.0, timer.seconds, "error"))
            out.stopped, out.note = "error", f"The AI call failed: {exc}"
            return out

        usage = getattr(response, "usage", None)
        spend.add(model, usage)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        usd = cost_usd(model, prompt, completion)
        out.calls += 1
        out.prompt_tokens += prompt
        out.completion_tokens += completion
        out.usd += usd
        out.cached_tokens += getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        billed = float(getattr(usage, "cost", 0) or 0)
        out.billed_usd += billed
        choice = response.choices[0]
        message = choice.message
        asked = message.tool_calls or []
        opened: list[int] = []

        def logged(outcome: str) -> None:
            # The cost report keeps what was billed: the price list knows nothing
            # of the cache, and an agent's prompt is mostly cached.
            call_log.add(call_log.Call(step, model, manufacturer, books or [], opened, product_keys or [],
                                       prompt, completion, billed or usd, timer.seconds, outcome))

        if not asked:
            if choice.finish_reason == "length":
                logged("cut_off")
                out.stopped, out.note = "cut_off", "The model stopped at its token limit"
                return out
            logged("ok")
            if final is None or nudged:
                out.stopped, out.note = "no_answer", (message.content or "")[:500]
                return out
            # Answered in words instead of through its final skill: asked once more.
            nudged = True
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append({"role": "user", "content": f"Hand in your answer with {final.name}."})
            continue

        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments or "{}"}}
                                        for c in asked]})
        pictures: list[dict[str, Any]] = []
        answered = False
        for c in asked:
            skill = by_name.get(c.function.name)
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            if args is None and choice.finish_reason == "length":
                reply = Reply(f"Your call to {c.function.name} was cut off at the reply limit. "
                              "Send it again in smaller pieces.", error=True)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": reply.text})
                out.turns.append(Turn(n, c.function.name, {}, reply.text, []))
                continue
            if skill is None:
                reply = Reply(f"Error: there is no skill called {c.function.name}.", error=True)
            elif not isinstance(args, dict):
                reply = Reply("Error: the arguments were not a JSON object.", error=True)
            elif answered:
                reply = Reply("Ignored: the answer was already handed in.")
            else:
                try:
                    reply = await skill.run(args)
                except Exception as exc:  # noqa: BLE001 - told to the model, which may ask again
                    log.warning("agent %s: skill %s failed: %s", step, skill.name, exc)
                    reply = Reply(f"Error: {exc}", error=True)
            if reply.image is not None:
                if out.images >= max_images:
                    reply = Reply("No more page pictures can be sent; use read_page for the text.",
                                  pages=reply.pages)
                else:
                    out.images += 1
                    pictures.append({"type": "text", "text": reply.image_label})
                    pictures.append({"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(reply.image).decode()}})
            opened.extend(reply.pages)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": reply.text})
            turn = Turn(n, c.function.name, args if isinstance(args, dict) else {},
                        reply.text[:200], reply.pages)
            out.turns.append(turn)
            if on_turn is not None:
                try:
                    await on_turn(turn)
                except Exception:  # noqa: BLE001 - a lost progress line must not stop the agent
                    log.exception("agent %s: progress not recorded", step)
            if skill is not None and skill.final and not reply.error and isinstance(args, dict):
                out.answer, answered = args, True
        logged("ok")
        if answered:
            out.stopped = "answered"
            return out
        if pictures:
            # Tool replies carry text only; the pictures follow as the user's message.
            messages.append({"role": "user", "content": pictures})

    out.stopped, out.note = "step_cap", f"No answer within {max_steps} steps"
    return out
