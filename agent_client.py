"""
AZM Agent Studio client — the ONLY module that knows the vendor HTTP contract.
Everything downstream consumes Turn, never raw JSON.

Verified from live traffic against the tarekat-testing tenant (2026-08-16):
  POST /api/v1/conversations                               -> {"id": uuid}
  POST /api/v1/chat/completions/stream?conversation_id=..  -> SSE, body {"message": str}

SSE frame shapes (all frames are `data: <json>`):
  {"step": {"kind": "tool_call",   "tool": "search_knowledge_base", "seq": 1}}
  {"step": {"kind": "tool_result", "tool": "search_knowledge_base", "seq": 1, "ok": true}}
  {"content": "<answer text>"}
  {"done": true, "message_id": uuid, "run_id": uuid, "sources": [],
   "pii_intercept_count": 0, "handoff_status": null,
   "abstention_reason": "clarification_requested"}   # abstention_reason optional

Contract change observed 2026-09-01 (widget endpoint, tarekat-testing):
  * `content` is now TOKEN-STREAMED — many small frames per answer, where it used to be
    one blob. `content_frames > 1` is therefore normal now and no longer signals
    anything unusual.
  * The `done` frame gained five keys, all previously dropped into unknown_keys:
      "retrieved_chunks": [{"content": str, ...}]   the text the answer was grounded in
      "answer_quality_score": float|null            0..1, the platform's own score
      "retrieval_score": float|null                 0..1
      "faithfulness_score": float|null              null under scoring_version v1
      "scoring_version": "v1-retrieval-only"
  * CRITICAL: `sources` now comes back EMPTY even when retrieval succeeded — the
    evidence moved to `retrieved_chunks`. Any check that reads "sources == [] means the
    answer was ungrounded" is now WRONG. Scores this good-looking are also the platform
    grading itself; treat them as a signal, not a verdict.

Contract change observed 2026-09-02 (widget endpoint, tarekat-testing):
  * A failed generation streams {"error": "stream_failed"} and NOTHING else — no
    content frame, no done frame. The HTTP status is still 200, so an error is only
    visible in the frame body. Before this key was known it landed in unknown_keys and
    the turn looked like an ordinary empty answer.
  * Seen when the agent's model_ref points at a batch-only model id (a `:batch` suffix,
    e.g. `google/gemini-3.7-flash:batch`), which cannot serve a streaming completion.
    So `error` usually means agent misconfiguration, not a transient network fault —
    check the agent's model before re-running.
  * `turn.error` is the field to check. A turn with `error` set carries no answer and
    MUST NOT be counted as a successful reply or diffed as though the bot stayed silent.

Contract change observed 2026-09-03 (widget endpoint, tarekat-testing):
  * The `done` frame gained "handoff_offer": a BOOLEAN, not an object.
      {"done": true, ..., "abstention_reason": "below_faithfulness",
       "handoff_offer": true}
  * It is present-and-true ONLY on an abstention, and absent entirely otherwise —
    there is no `false` form, so read it as "absent means no offer".
  * It is NOT the same field as `handoff_status`, which stays null on these turns.
    `handoff_status` is a handoff that HAPPENED; `handoff_offer` is the bot offering
    one and waiting for the user to take it. Never fold the two together: a turn can
    offer a human and still be handled entirely in-bot.
  * Before this key was known, every abstention turn silently lost the fact that the
    user was offered an escape hatch — which is the difference between "the bot was
    unhelpful" and "the bot was unhelpful but routed the citizen to a human".

A write-capable tool does NOT execute. It streams a human-in-the-loop gate instead,
and the turn carries NO content frame at all (2026-08-18):
  {"confirmation_request": {"pending_action_id": uuid, "tool": "send_iban_reminder",
   "arguments": {...}, "question": str, "parameters": [{"label","value"}, ...],
   "target": "Tarekat Support (Mock Studio) / send_iban_reminder"}}
  {"done": true, ..., "abstention_reason": "awaiting_confirmation"}
Approving is a separate call the widget makes with pending_action_id — replying "نعم"
in chat just re-issues a fresh confirmation_request, so this client cannot complete
the action. Treat `turn.confirmation_request` as "the bot asked to act", never as
"the bot acted".

Turn.unknown_keys is the drift detector: if the vendor adds a field, it shows up
there and the contract test fails loudly instead of the data silently going missing.
"""

import json
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("AGENT_API", "https://api.chatbot-studio.azm-cit.com/api/v1")
KEY = os.environ["AGENT_API_KEY"]
ORIGIN = os.environ.get("AGENT_ORIGIN", "https://qa-merath.web.azm-dev.com")

# Keys we know how to consume. Anything else lands in Turn.unknown_keys.
KNOWN_FRAME_KEYS = {"step", "content", "done", "message_id", "run_id", "sources",
                    "pii_intercept_count", "handoff_status", "abstention_reason",
                    "confirmation_request",
                    # Added by the vendor and first seen 2026-09-01 — see the module
                    # docstring. Before this they were silently discarded.
                    "retrieved_chunks", "answer_quality_score", "retrieval_score",
                    "faithfulness_score", "scoring_version",
                    # Failed generation, first seen 2026-09-02 — see the module
                    # docstring. Arrives alone, with HTTP 200 and no done frame.
                    "error",
                    # Bot offered a human handoff, first seen 2026-09-03 — see the
                    # module docstring. Boolean, abstention turns only.
                    "handoff_offer"}


@dataclass
class Turn:
    """Normalized result. Maps 1:1 onto a ragas SingleTurnSample."""
    user_input: str
    response: str = ""
    retrieved_contexts: list[str] = field(default_factory=list)

    ttfb_ms: int = 0            # first frame of ANY kind (usually the tool_call step)
    first_content_ms: int = 0   # first frame carrying answer text — the real "user sees words"
    total_ms: int = 0

    conversation_id: str = ""
    message_id: str = ""
    run_id: str = ""            # look this up in Studio → Runs & Traces

    steps: list[dict] = field(default_factory=list)   # tool_call / tool_result trace
    content_frames: int = 0     # >1 means real token streaming; 1 means one big blob
    pii_intercept_count: int = 0
    handoff_status: str | None = None
    abstention_reason: str | None = None

    # The bot OFFERED a human handoff (2026-09-03 contract). Arrives true only on
    # abstention turns and is absent otherwise, so None means "no offer was made".
    # Distinct from handoff_status, which is a handoff that actually occurred — an
    # offered handoff is still an in-bot turn.
    handoff_offer: bool | None = None

    # Set when the agent wants to run a write-capable tool and is waiting for the
    # user to approve it. A turn with this set has no answer text — that is correct
    # behaviour, not an empty response.
    confirmation_request: dict | None = None

    # Grounding evidence and self-scores (2026-09-01 contract). `sources` now arrives
    # EMPTY while the retrieved text lands in `retrieved_chunks`, so an empty `sources`
    # no longer means "nothing was retrieved" — check retrieved_chunks before calling
    # any answer ungrounded.
    retrieved_chunks: list[dict] = field(default_factory=list)
    answer_quality_score: float | None = None
    retrieval_score: float | None = None
    faithfulness_score: float | None = None
    scoring_version: str | None = None

    # Set when the vendor streamed {"error": ...} instead of generating. The turn has
    # no answer text and no done frame. This is a FAILED turn, not a silent bot — never
    # diff it against a production answer as though the bot chose to say nothing.
    error: str | None = None

    unknown_keys: set[str] = field(default_factory=set)

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def searched_kb(self) -> bool:
        return any(s.get("tool") == "search_knowledge_base" for s in self.steps)

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmation_request is not None


def _extract(frame: dict, turn: Turn) -> None:
    """Pull everything we care about out of one decoded SSE frame."""
    if (step := frame.get("step")) is not None:
        turn.steps.append(step)

    if isinstance(content := frame.get("content"), str):
        turn.response += content
        turn.content_frames += 1

    if isinstance(confirm := frame.get("confirmation_request"), dict):
        turn.confirmation_request = confirm

    if (err := frame.get("error")) is not None:
        turn.error = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)

    if frame.get("done"):
        turn.message_id = frame.get("message_id") or ""
        turn.run_id = frame.get("run_id") or ""
        turn.pii_intercept_count = frame.get("pii_intercept_count") or 0
        turn.handoff_status = frame.get("handoff_status")
        turn.abstention_reason = frame.get("abstention_reason")
        turn.handoff_offer = frame.get("handoff_offer")
        turn.answer_quality_score = frame.get("answer_quality_score")
        turn.retrieval_score = frame.get("retrieval_score")
        turn.faithfulness_score = frame.get("faithfulness_score")
        turn.scoring_version = frame.get("scoring_version")

        for src in frame.get("sources") or []:
            turn.retrieved_contexts.append(
                src if isinstance(src, str)
                else (src.get("text") or src.get("content") or json.dumps(src, ensure_ascii=False))
            )

        # The retrieved text now arrives here rather than in `sources`. Feed it into
        # retrieved_contexts too, so ragas-shaped consumers keep working unchanged.
        for ch in frame.get("retrieved_chunks") or []:
            if isinstance(ch, dict):
                turn.retrieved_chunks.append(ch)
                text = ch.get("content") or ch.get("text")
            else:
                turn.retrieved_chunks.append({"content": str(ch)})
                text = str(ch)
            if text:
                turn.retrieved_contexts.append(text)

    turn.unknown_keys |= set(frame) - KNOWN_FRAME_KEYS


class AgentClient:
    def __init__(self, timeout: float = 60.0, origin: str | None = ORIGIN,
                 api_key: str = KEY):
        headers = {"x-api-key": api_key, "content-type": "application/json"}
        if origin:                      # widget keys are origin-checked; pass None to test that
            headers["origin"] = origin
            headers["referer"] = origin + "/"
        self._c = httpx.Client(base_url=BASE, timeout=timeout, headers=headers)

    def new_conversation(self) -> str:
        r = self._c.post("/conversations", content=b"")
        r.raise_for_status()
        d = r.json()
        cid = d.get("id") or d.get("conversation_id") or d.get("data", {}).get("id")
        if not cid:
            raise RuntimeError(f"no conversation id in response: {d}")
        return cid

    def ask(self, message: str, conversation_id: str | None = None) -> Turn:
        """One user message. Pass conversation_id to continue a thread (multi-turn tests)."""
        cid = conversation_id or self.new_conversation()
        turn = Turn(user_input=message, conversation_id=cid)
        t0 = time.perf_counter()

        with self._c.stream(
            "POST", "/chat/completions/stream",
            params={"conversation_id": cid},
            json={"message": message},
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                if not turn.ttfb_ms:
                    turn.ttfb_ms = int((time.perf_counter() - t0) * 1000)
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    turn.response += payload      # plain-text stream — contract drift
                    turn.unknown_keys.add("<non-json frame>")
                    continue
                _extract(frame, turn)
                if turn.content_frames and not turn.first_content_ms:
                    turn.first_content_ms = int((time.perf_counter() - t0) * 1000)

        turn.total_ms = int((time.perf_counter() - t0) * 1000)
        return turn

    def conversation(self) -> "Thread":
        return Thread(self)

    def close(self) -> None:
        self._c.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class Thread:
    """Sticky conversation id — for multi-turn / context-retention tests."""

    def __init__(self, client: AgentClient):
        self._client = client
        self.id = client.new_conversation()
        self.turns: list[Turn] = []

    def ask(self, message: str) -> Turn:
        turn = self._client.ask(message, conversation_id=self.id)
        self.turns.append(turn)
        return turn


def probe(message: str = "كيف اقدم حصر ورثة") -> None:
    """Print raw frames. Re-run whenever the vendor deploys, to catch contract drift."""
    with AgentClient() as c:
        cid = c.new_conversation()
        print("conversation_id:", cid)
        with c._c.stream("POST", "/chat/completions/stream",
                         params={"conversation_id": cid},
                         json={"message": message}) as r:
            print("status:", r.status_code, "| content-type:", r.headers.get("content-type"))
            for i, line in enumerate(r.iter_lines()):
                if line:
                    print(f"[{i}] {line[:400]}")


if __name__ == "__main__":
    probe()
