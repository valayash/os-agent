"""The agent's entire memory (CLAUDE.md §7).

WHY A TypedDict AND NOT A DATACLASS
-----------------------------------
LangGraph merges *partial* updates: a node returns a dict of only the keys it
changed, and the graph applies it over the current state. A TypedDict is the
natural shape for that. It also means the node contract is enforceable by
reading a function's return type.

WHY THERE ARE NO REDUCERS HERE — this is the whole project in one detail
-----------------------------------------------------------------------
LangGraph channels default to OVERWRITE: the new value replaces the old one.
To accumulate instead, you opt in with a reducer, e.g.

    messages: Annotated[list, add_messages]     # <- we never write this

That single line is what makes published agents get slower every step: the
prompt grows, time-to-first-token grows with it, and step 40 costs 3x step 1.
Our design is LangGraph's DEFAULT behaviour. Published agents deliberately opt
into the thing that kills them, because it is the obvious way to give a model
context. We get context from `facts` instead — ten strings, hard capped.

So: every field below is either a scalar or a hard-capped list, and the prompt
is REBUILT from state each step, never appended to (§4.2, §11).
"""

from typing import TypedDict

from os_agent.types import Action, Element, Expectation

# Bounded bookkeeping (§5.2): these never enter the prompt, so they only need
# to be bounded, not fixed-size.
HASH_RING = 8  # steps of loop-detection history
LOOP_REPEATS = 3  # same (screen, action) this many times -> forced reflect
FACTS_CAP = 10  # enters the prompt, so this one is FIXED-size
MAX_CONSECUTIVE_FAILURES = 2  # §11: never retry a failing action more than twice
MAX_REFLECTIONS = 3  # reflect is an extra LLM call; it is not free
REFLECTION_CAP = 240  # characters. It enters the prompt, so it is capped.


class AgentState(TypedDict, total=False):
    # -- the task ----------------------------------------------------------
    goal: str
    task_id: str

    # -- the CURRENT screen only. Never a history, never a list of these. ---
    app: str
    bundle_id: str
    elements: list[Element]
    screenshot_b64: str  # ANNOTATED — what the planner sees
    screenshot_plain_b64: str  # clean — evidence for the pixel diff
    obs_fresh: bool  # verify captured already; observe is then a no-op (§8.8)
    screen_hash: str  # fingerprint of the element set, for loop detection

    # -- what just happened -------------------------------------------------
    subgoal: str
    reasoning: str
    last_action: Action | None
    last_expect: Expectation | None
    last_outcome: str  # success | no_change | error | ambiguous
    # Set by execute when the action could not run (stale id, bad key, ...).
    # Read by verify, then cleared. A scalar, so it cannot accumulate (§4.2).
    executor_error: str
    last_reason: str  # the one-line why, e.g. "'Save' did not appear"

    # -- compressed memory. ENTERS THE PROMPT, so hard-capped at 10. --------
    facts: list[str]
    # ONE slot, overwritten each time — a strategy correction, not a history.
    # Facts are durable truths about the world; this is transient advice about
    # approach. Both are fixed-size, so the prompt never grows (§4.2).
    reflection_note: str

    # -- counters ----------------------------------------------------------
    step: int
    llm_calls: int  # the numerator of THE metric (§1)
    consecutive_failures: int
    reflections: int
    cost_usd: float

    # -- bounded bookkeeping. NOT in the prompt. ---------------------------
    recent_hashes: list[str]  # ring(8) of hash(screen, action)
    looping: bool  # loop detector fired this step
    perception_ms: float  # observe cost, for the latency-vs-step curve

    # -- termination -------------------------------------------------------
    status: str  # running | done | failed
    terminal_reason: str  # done | max_steps | stuck | budget | error | agent_fail


def initial_state(goal: str, task_id: str = "freeform") -> AgentState:
    """Every field explicitly initialised.

    LangGraph tolerates missing keys, but a node that reads one and gets None
    fails in a way that points at the node rather than at the omission here.
    Paying for explicitness once beats debugging it five times.
    """
    return AgentState(
        goal=goal,
        task_id=task_id,
        app="",
        bundle_id="",
        elements=[],
        screenshot_b64="",
        screenshot_plain_b64="",
        obs_fresh=False,
        screen_hash="",
        subgoal="",
        reasoning="",
        last_action=None,
        last_expect=None,
        last_outcome="",
        executor_error="",
        last_reason="",
        facts=[],
        reflection_note="",
        step=0,
        llm_calls=0,
        consecutive_failures=0,
        reflections=0,
        cost_usd=0.0,
        recent_hashes=[],
        looping=False,
        perception_ms=0.0,
        status="running",
        terminal_reason="",
    )


def push_fact(facts: list[str], new: str | None) -> list[str]:
    """Append one fact, dedup, FIFO-evict at the cap.

    Returns a NEW list — nodes must never mutate state in place (§8.8), or
    LangGraph's checkpointer records a state that already contains the change
    and time-travel debugging silently lies to you.
    """
    if not new or new in facts:
        return facts
    return (facts + [new])[-FACTS_CAP:]


def push_hash(ring: list[str], h: str) -> list[str]:
    return (ring + [h])[-HASH_RING:]


def is_looping(ring: list[str], h: str) -> bool:
    """Same (screen, action) pair seen LOOP_REPEATS times in the ring.

    `consecutive_failures` catches an action that keeps FAILING. This catches
    the nastier case: an agent oscillating between two screens where every
    action reports SUCCESS and nothing progresses. Without this, such a run
    burns all 50 steps and the whole budget while looking healthy in the logs.
    """
    return (ring + [h]).count(h) >= LOOP_REPEATS
