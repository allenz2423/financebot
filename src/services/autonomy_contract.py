"""Pure, deterministic turn-intent contracts for Delilah.

Only the user message and explicit trusted controller flags are inputs. Any
quoted text, retrieved content, or caller-supplied task context must be treated
as untrusted and must not be passed as authority. This module performs no I/O
and does not persist contracts.

Conservative policy choices: broad financial language does not imply a Plaid
refresh unless it is one of the recognized open-ended requests; a continuation
does not itself authorize new actions; and every sync allowed here is strictly
pull-only. Missing, extra, or non-boolean sync arguments are rejected. No
destructive tool is ever permitted by ``mutation_allowed``.

Directly requested non-Plaid mutations are distinguished in
``explicit_mutation_intent`` for transparent handling, but are not granted:
this module has no per-tool argument/target mappings for them. A future
tool-specific mapping must validate the exact target and still defer to the
normal approval, authorization, and receipt controls.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Literal, Mapping


IntentMode = Literal[
    "execute",
    "investigate_report",
    "plan",
    "monitor",
    "continue",
    "await_decision",
]


@dataclass(frozen=True, slots=True)
class MutationPermission:
    """A narrowly scoped permission; arguments are an exact key/value set."""

    tool_name: str
    exact_arguments: tuple[tuple[str, object], ...]
    target_scope: str | None = None
    target_value: str | None = None


@dataclass(frozen=True, slots=True)
class AutonomyContract:
    """Immutable, deterministic statement of one turn's intended scope."""

    mode: IntentMode
    objective: str
    allowed_initiative: tuple[str, ...]
    required_evidence: tuple[str, ...]
    completion_criteria: tuple[str, ...]
    mutation_permissions: tuple[MutationPermission, ...]
    required_tools: frozenset[str]
    explicit_mutation_intent: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation with stable field names."""

        return {
            "mode": self.mode,
            "objective": self.objective,
            "allowed_initiative": list(self.allowed_initiative),
            "required_evidence": list(self.required_evidence),
            "completion_criteria": list(self.completion_criteria),
            "mutation_permissions": [
                {
                    "tool_name": permission.tool_name,
                    "exact_arguments": dict(permission.exact_arguments),
                    "target_scope": permission.target_scope,
                    "target_value": permission.target_value,
                }
                for permission in self.mutation_permissions
            ],
            "required_tools": sorted(self.required_tools),
            "explicit_mutation_intent": self.explicit_mutation_intent,
        }

    @property
    def status_summary(self) -> str:
        """Short scope statement suitable for a user-visible status surface."""

        if self.required_tools == _FINANCIAL_REVIEW_TOOLS:
            return (
                f"{self.objective} I will not pay bills, transfer funds, reconcile records, "
                "or make other account changes."
            )
        if self.mode == "execute" and self.required_tools == frozenset({"sync_plaid_accounting"}):
            return "Run the requested Plaid refresh only; do not reconcile or send a summary."
        return self.objective


_PLAID_SYNC_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:"
    r"(?:run|start|perform|do|execute|initiate|trigger|launch|refresh|update|pull)"
    r"\s+(?:a\s+)?plaid\s+sync"
    r"|sync\s+(?:my\s+)?plaid(?:\s+accounts?)?(?:\s+(?:now|please|today))?"
    r"|plaid\s+sync(?:\s+(?:now|please|today))?"
    r")\s*[.!?]*$",
    re.IGNORECASE,
)
_NEGATED_PLAID_SYNC = re.compile(
    r"\b(?:don't|do not|never|without)\b[^.!?\n]{0,48}"
    r"\b(?:plaid\s+sync|sync\s+plaid)\b",
    re.IGNORECASE,
)
_VAGUE_REVIEW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # These are complete request forms, not loose phrase matches. That
        # keeps explanatory/quoted mentions from initiating a refresh.
        r"^\s*(?:please\s+)?(?:just\s+)?do\s+whatever\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:just\s+)?take\s+care\s+of\s+(?:this|it|my\s+finances)\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:just\s+)?(?:review|look\s+at)\s+my\s+(?:financial\s+)?situation\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:just\s+)?(?:review|look\s+over|assess)\s+(?:everything|all\s+of\s+it|my\s+finances|my\s+financial\s+situation)\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:just\s+)?(?:what\s+should\s+i|what\s+do\s+i\s+need\s+to)\s+focus\s+on\s+(?:with|for)\s+my\s+finances\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?help\s+me\s+(?:with|manage|organize|sort\s+out)\s+my\s+finances\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:just\s+)?(?:take\s+care\s+of|handle|sort\s+out)\s+my\s+financial\s+(?:situation|life|affairs)\s*[.!?]*$",
        r"^\s*(?:can|could|would)\s+you\s+(?:please\s+)?(?:review|look\s+over|assess)\s+(?:everything|all\s+of\s+it|my\s+finances|my\s+financial\s+situation)\s*[.!?]*$",
    )
)
_STANDALONE_REVIEW_ALL = re.compile(
    r"^\s*(?:please\s+)?(?:just\s+)?(?:review|look\s+over|assess)\s+everything\s*[.!?]*$",
    re.IGNORECASE,
)
_COMPOUND_FINANCIAL_REVIEW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(?:please\s+)?(?:can|could|would)\s+you\s+(?:please\s+)?do\s+whatever\s+(?:is\s+)?best\s+with\s+my\s+finances\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:review|look\s+over|assess)\s+my\s+financial\s+situation\s+and\s+(?:suggest|recommend|identify|tell\s+me|show\s+me)\b[^.!?]*[.!?]*$",
        r"^\s*(?:please\s+)?what\s+should\s+i\s+do\s+with\s+my\s+finances\s*[.!?]*$",
        r"^\s*(?:please\s+)?(?:take\s+a\s+look\s+at|review|assess)\s+my\s+finances\s+and\s+(?:suggest|recommend|identify|tell\s+me|show\s+me)\b[^.!?]*[.!?]*$",
    )
)
_EXPLANATORY_REVIEW_MENTION = re.compile(
    r"\b(?:what\s+does|explain|meaning\s+of|the\s+phrase|"
    r"what\s+is\s+meant\s+by|not\s+asking\s+you\s+to)\b",
    re.IGNORECASE,
)
_FINANCIAL_CONTEXT = re.compile(
    r"\b(?:financ\w*|money|cash|checking|savings|bank|account balance|"
    r"credit card|debt|loan|bill|budget|spending|transaction|plaid|"
    r"income|paycheck|investment|portfolio|subscription)\b",
    re.IGNORECASE,
)
_AWAIT_DECISION = re.compile(
    r"\b(?:await(?:ing)?\s+(?:your\s+)?(?:decision|approval|choice)|"
    r"need\s+your\s+(?:decision|choice|approval)|"
    r"which\s+(?:one|option)\s+should\s+i|"
    r"should\s+i\s+(?:proceed|continue|choose|approve)|"
    r"do\s+you\s+(?:want|approve)|"
    r"waiting\s+for\s+your\s+(?:decision|approval))\b",
    re.IGNORECASE,
)
_MONITOR = re.compile(
    r"\b(?:monitor|keep\s+an\s+eye\s+on|watch\s+for|"
    r"alert\s+me\s+(?:when|if)|notify\s+me\s+(?:when|if))\b",
    re.IGNORECASE,
)
_EXPLICIT_MONITOR_CREATE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"(?:(?:monitor|watch|keep\s+an\s+eye\s+on)\b.{1,500}"
    r"(?:alert|notify)\s+me\s+(?:when|if)\b.{1,300}|"
    r"(?:alert|notify)\s+me\s+(?:when|if)\b.{1,500})[.!?]*\s*$",
    re.IGNORECASE,
)
_PLAN = re.compile(
    r"\b(?:plan|make\s+(?:me\s+)?a\s+plan|help\s+me\s+decide|"
    r"what\s+should\s+i\s+do|how\s+should\s+i)\b",
    re.IGNORECASE,
)
_CONTINUE = re.compile(
    r"\b(?:continue|resume|pick\s+up|carry\s+on|keep\s+going|"
    r"where\s+were\s+we)\b",
    re.IGNORECASE,
)
_DIRECT_MUTATION_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"(?P<verb>pay|transfer|delete|remove|cancel|create|update|send|reconcile|add|set|schedule|fund|lock|tag|save)\b"
    r"(?P<target>[^.!?\n]{1,160})[.!?]*\s*$",
    re.IGNORECASE,
)
_TASK_CANCEL_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    r"(?:cancel|stop)\s+(?:(?:my|the|this)\s+)?(?:background\s+)?task"
    r"(?:\s+(?:id\s+)?(?P<task_id>(?!now\b|please\b)[A-Za-z0-9_-]{4,80}))?"
    r"(?:\s+(?:now|please))?\s*[.!?]*$",
    re.IGNORECASE,
)
_TASK_STEER_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:steer|correct|redirect)\s+(?:task\s+)?step\s+"
    r"(?P<step_id>[A-Za-z0-9_.:-]{1,128})\s*:\s*"
    r"(?P<correction>[\s\S]{2,500})\s*$",
    re.IGNORECASE,
)
_PROFILE_FIELD_NAMES = (
    "risk_tolerance|autonomy_limits|notification_preferences|"
    "preferred_model|presentation_style|inferred_preference_learning"
)
_PROFILE_SET_REQUEST = re.compile(
    r"^\s*(?:please\s+)?set\s+my\s+(?:operating\s+)?profile\s+field\s+"
    rf"(?P<field>{_PROFILE_FIELD_NAMES})\s+(?:to|=)\s*(?P<value>[\s\S]{{1,500}}?)"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_PROFILE_FORGET_REQUEST = re.compile(
    r"^\s*(?:please\s+)?forget\s+my\s+(?:operating\s+)?profile"
    rf"(?:\s+field\s+(?P<field>{_PROFILE_FIELD_NAMES}))?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_PULL_ONLY_PERMISSION = MutationPermission(
    tool_name="sync_plaid_accounting",
    exact_arguments=(
        ("force_refresh", True),
        ("reconcile", False),
        ("post_summary", False),
    ),
)
_FINANCIAL_REVIEW_TOOLS = frozenset(
    {
        "sync_plaid_accounting",
        "get_current_financial_position",
        "get_debt_overview",
        "get_upcoming_bills_calendar",
    }
)
_FINANCIAL_REVIEW_ORDER = (
    "sync_plaid_accounting",
    "get_current_financial_position",
    "get_debt_overview",
    "get_upcoming_bills_calendar",
)
_DESTRUCTIVE_PREFIXES = (
    "delete_",
    "remove_",
    "cancel_",
    "transfer_",
    "pay_",
    "send_",
    "create_",
    "update_",
    "reconcile_",
    "monitor_add_",
    "monitor_create_",
    "monitor_clear_",
)


def _contract(
    mode: IntentMode,
    objective: str,
    *,
    initiative: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    completion: tuple[str, ...] = (),
    permissions: tuple[MutationPermission, ...] = (),
    required_tools: frozenset[str] = frozenset(),
    explicit_mutation_intent: str | None = None,
) -> AutonomyContract:
    return AutonomyContract(
        mode=mode,
        objective=objective,
        allowed_initiative=initiative,
        required_evidence=evidence,
        completion_criteria=completion,
        mutation_permissions=permissions,
        required_tools=required_tools,
        explicit_mutation_intent=explicit_mutation_intent,
    )


def _is_open_ended_financial_review(text: str) -> bool:
    if _EXPLANATORY_REVIEW_MENTION.search(text):
        return False
    return any(
        pattern.fullmatch(text)
        for pattern in (*_VAGUE_REVIEW_PATTERNS, *_COMPOUND_FINANCIAL_REVIEW_PATTERNS)
    )


def _explicit_unmapped_mutation(text: str) -> str | None:
    """Describe a direct mutation request without turning it into authority.

    At present only the exact pull-only Plaid refresh has a safe tool/argument
    mapping. Other direct mutation requests are tagged for transparent
    handling, but remain denied until a tool-specific mapping can bind the
    exact operation and target. This is intentionally not a generic grant.
    """

    match = _DIRECT_MUTATION_REQUEST.fullmatch(text)
    if not match:
        return None
    verb = match.group("verb").casefold()
    target = " ".join(match.group("target").split())
    if not target or target.casefold() in {"it", "that", "this"}:
        return f"unmapped:{verb}"
    # Do not copy arbitrary user text into the contract/status surface. The
    # operation verb is enough to distinguish direct intent; tool-specific
    # code must still derive and validate the exact target before permission.
    return f"unmapped:{verb}"


def build_autonomy_contract(
    user_message: str,
    *,
    financial_context: bool = False,
    recent_user_context: str = "",
    has_active_task: bool = False,
) -> AutonomyContract:
    """Build a pure contract from a user turn and trusted controller flags.

    ``financial_context``, ``recent_user_context``, and ``has_active_task``
    must come from trusted application state. Recent context must contain only
    owner/conversation-scoped user messages, never assistant or tool output.
    A financial context can also be recognized from explicit financial terms
    in the current user message.
    """

    text = str(user_message or "").strip()
    financial = (
        financial_context
        or bool(_FINANCIAL_CONTEXT.search(text))
        or bool(_FINANCIAL_CONTEXT.search(str(recent_user_context or "")))
    )

    if _AWAIT_DECISION.search(text):
        return _contract(
            "await_decision",
            "Obtain the user's specific decision before proceeding.",
            evidence=("A clear answer to the pending decision",),
            completion=("Record the decision as input; do not treat it as broader approval",),
        )

    profile_set_match = _PROFILE_SET_REQUEST.fullmatch(text)
    if profile_set_match:
        field = profile_set_match.group("field").casefold()
        raw_value = profile_set_match.group("value").strip()
        try:
            if field in {"autonomy_limits", "notification_preferences"}:
                value = json.loads(raw_value)
            elif field == "inferred_preference_learning":
                parsed = raw_value.casefold()
                value = True if parsed == "true" else False if parsed == "false" else None
            elif field in {"risk_tolerance", "presentation_style"}:
                value = raw_value.casefold()
            else:
                value = raw_value
        except (TypeError, ValueError):
            value = None
        if value is not None:
            values = {field: value}
            return _contract(
                "execute",
                "Change only the exact operating-profile field the user named.",
                evidence=("A persisted owner-scoped profile value and provenance",),
                completion=("Report the exact profile field that changed",),
                permissions=(MutationPermission(
                    "manage_user_profile",
                    (("action", "set"), ("values", values)),
                    target_scope="user_profile",
                ),),
                required_tools=frozenset({"manage_user_profile"}),
                explicit_mutation_intent="manage_user_profile",
            )

    profile_forget_match = _PROFILE_FORGET_REQUEST.fullmatch(text)
    if profile_forget_match:
        field = profile_forget_match.group("field")
        exact_arguments = {"action": "forget"}
        if field:
            exact_arguments["fields"] = [field.casefold()]
        return _contract(
            "execute",
            "Forget only the exact operating-profile field or profile requested by the user.",
            evidence=("An owner-scoped profile deletion result",),
            completion=("Report which profile field or profile was forgotten",),
            permissions=(MutationPermission(
                "manage_user_profile",
                tuple(sorted(exact_arguments.items())),
                target_scope="user_profile",
            ),),
            required_tools=frozenset({"manage_user_profile"}),
            explicit_mutation_intent="manage_user_profile",
        )

    task_cancel_match = _TASK_CANCEL_REQUEST.fullmatch(text)
    if task_cancel_match:
        requested_task_id = task_cancel_match.group("task_id")
        return _contract(
            "execute",
            "Cancel only a task in this owner's visible conversation-scoped task list.",
            initiative=("Inspect the visible task list to identify the requested task",),
            evidence=("A task-cancellation result scoped to this conversation",),
            completion=("Report whether cancellation was recorded or reconciliation is needed",),
            permissions=(
                MutationPermission(
                    "task_cancel",
                    (),
                    target_scope="conversation_task",
                    target_value=requested_task_id,
                ),
            ),
            required_tools=frozenset({"task_cancel"}),
            explicit_mutation_intent="task_cancel",
        )

    task_steer_match = _TASK_STEER_REQUEST.fullmatch(text)
    if task_steer_match:
        step_id = task_steer_match.group("step_id")
        correction = task_steer_match.group("correction").strip()
        return _contract(
            "execute",
            "Apply the user's guidance only to the named unstarted task step.",
            initiative=("Inspect the exact conversation-scoped task step before steering",),
            evidence=("A persisted correction event linked to the exact step ID",),
            completion=("Report whether guidance was recorded; do not claim the step ran",),
            permissions=(
                MutationPermission(
                    "steer_task",
                    (("step_id", step_id), ("correction", correction)),
                    target_scope="task_step",
                    target_value=step_id,
                ),
            ),
            required_tools=frozenset({"steer_task"}),
            explicit_mutation_intent="steer_task",
        )

    if _MONITOR.search(text):
        if _EXPLICIT_MONITOR_CREATE.search(text):
            return _contract(
                "monitor",
                "Create only the monitor rule explicitly requested in this message.",
                initiative=("Translate the exact requested condition into one monitor rule",),
                evidence=("A confirmed owner-scoped monitor-rule receipt",),
                completion=("Report the created rule or stop for reconciliation if its outcome is uncertain",),
                permissions=(
                    MutationPermission(
                        "monitor_create_natural_rule",
                        (("instruction", text),),
                        target_scope="exact_instruction",
                    ),
                ),
                required_tools=frozenset({"monitor_create_natural_rule"}),
            )
        return _contract(
            "monitor",
            "Review existing monitoring only; do not create or change rules.",
            initiative=("Inspect relevant existing monitor state",),
            evidence=("Owner-scoped monitor state or source data",),
            completion=("Report the monitoring status; do not create or change rules",),
        )

    if has_active_task and _CONTINUE.search(text):
        return _contract(
            "continue",
            "Resume the existing active task within its saved scope.",
            initiative=("Inspect the active task and continue its next safe step",),
            evidence=("Active task state and receipts for completed steps",),
            completion=("Continue only within the existing task scope or surface a blocker",),
        )

    if _NEGATED_PLAID_SYNC.search(text):
        return _contract(
            "investigate_report",
            "Answer the user's request without running a Plaid sync.",
            initiative=("Use relevant read-only information only",),
            completion=("Answer from available evidence and disclose missing or stale data",),
        )

    if _PLAID_SYNC_REQUEST.search(text):
        return _contract(
            "execute",
            "Run the explicitly requested pull-only Plaid refresh.",
            initiative=("Refresh connected Plaid data without reconciliation or outbound summary",),
            evidence=("A tool receipt and resulting refresh status",),
            completion=("Report the observed sync result; do not claim success without evidence",),
            permissions=(_PULL_ONLY_PERMISSION,),
            required_tools=frozenset({"sync_plaid_accounting"}),
        )

    if (financial or _STANDALONE_REVIEW_ALL.fullmatch(text)) and _is_open_ended_financial_review(text):
        return _contract(
            "investigate_report",
            "Refresh financial data, inspect liquidity, debt, and upcoming bills, then recommend top actions.",
            initiative=(
                "Run only a pull-only Plaid refresh",
                "Inspect current financial position, debt, and upcoming bills",
                "Rank informational recommendations; do not execute them",
            ),
            evidence=(
                "Successful pull-only refresh receipt or an explicit refresh failure",
                "Current financial-position evidence and source freshness",
                "Debt overview evidence and source freshness",
                "Upcoming-bills evidence and source freshness",
            ),
            completion=(
                "Summarize supported findings and identify stale or missing sources",
                "Recommend prioritized next actions without performing them",
            ),
            permissions=(_PULL_ONLY_PERMISSION,),
            required_tools=_FINANCIAL_REVIEW_TOOLS,
        )

    if _PLAN.search(text):
        return _contract(
            "plan",
            "Prepare a plan or recommendation without executing it.",
            initiative=("Gather only information necessary to produce the requested plan",),
            evidence=("Relevant source data for recommendations",),
            completion=("Present an ordered plan and distinguish proposals from actions",),
        )

    explicit_mutation_intent = _explicit_unmapped_mutation(text)
    if explicit_mutation_intent is not None:
        return _contract(
            "execute",
            "The user explicitly requested a state-changing action, but no exact safe tool-and-target mapping exists; do not dispatch it.",
            completion=(
                "Explain that the requested action was not performed because its exact tool and target could not be safely mapped",
                "Ask for a specific supported operation or use the applicable approval flow",
            ),
            explicit_mutation_intent=explicit_mutation_intent,
        )

    return _contract(
        "investigate_report",
        "Answer the user's question using relevant evidence.",
        initiative=("Use the minimum relevant read-only information",),
        evidence=("Evidence sufficient to support the answer",),
        completion=("Answer directly and disclose uncertainty or missing evidence",),
    )


def mutation_allowed(
    contract: AutonomyContract,
    tool_name: str,
    arguments: Mapping[str, object],
    has_active_task: bool = False,
) -> bool:
    """Return whether a tool call exactly matches this contract's narrow grant.

    This helper is deliberately an additional hard gate, not a replacement
    for dispatch authorization, user approval, receipts, or tool-specific
    validation. ``has_active_task`` never expands the permissions in this
    initial contract implementation.
    """

    del has_active_task  # Active-task presence is not authorization.
    if not isinstance(contract, AutonomyContract) or not isinstance(arguments, Mapping):
        return False
    if contract.mode not in {"execute", "investigate_report", "monitor"}:
        return False
    for permission in contract.mutation_permissions:
        if permission.tool_name != tool_name or permission.target_scope is None:
            continue
        if permission.target_scope == "conversation_task":
            return (
                tool_name == "task_cancel"
                and set(arguments) == {"task_id"}
                and isinstance(arguments.get("task_id"), str)
                and bool(arguments.get("task_id", "").strip())
                and (
                    permission.target_value is None
                    or arguments.get("task_id") == permission.target_value
                )
            )
        if permission.target_scope == "task_step":
            expected = dict(permission.exact_arguments)
            supplied = dict(arguments)
            return (
                tool_name == "steer_task"
                and set(supplied) == {"task_id", "step_id", "correction"}
                and isinstance(supplied.get("task_id"), str)
                and bool(supplied.get("task_id", "").strip())
                and supplied.get("step_id") == permission.target_value
                and supplied.get("step_id") == expected.get("step_id")
                and supplied.get("correction") == expected.get("correction")
                and isinstance(supplied.get("correction"), str)
                and 1 <= len(supplied["correction"]) <= 500
            )
        if permission.target_scope == "user_profile":
            return (
                tool_name == "manage_user_profile"
                and dict(arguments) == dict(permission.exact_arguments)
            )
        if permission.target_scope == "exact_instruction":
            expected = dict(permission.exact_arguments)
            supplied = dict(arguments)
            return (
                tool_name == "monitor_create_natural_rule"
                and set(supplied) == {"instruction"}
                and isinstance(supplied.get("instruction"), str)
                and len(supplied["instruction"]) <= 1000
                and supplied == expected
            )
    if tool_name != "sync_plaid_accounting":
        return False
    if any(tool_name.startswith(prefix) for prefix in _DESTRUCTIVE_PREFIXES):
        return False

    expected = {"force_refresh": True, "reconcile": False, "post_summary": False}
    if set(arguments) != set(expected):
        return False
    if any(type(arguments[key]) is not bool for key in expected):
        return False
    if dict(arguments) != expected:
        return False

    return any(
        permission == _PULL_ONLY_PERMISSION and permission.tool_name == tool_name
        for permission in contract.mutation_permissions
    )


def tool_call_allowed_by_contract(
    contract: AutonomyContract,
    tool_name: str,
    arguments: Mapping[str, object],
    *,
    side_effect: str,
    control_tool: bool = False,
) -> bool:
    """Apply the contract at dispatch using authoritative effect metadata.

    Read-only tools remain usable for evidence gathering. Mutating, conditional,
    or external actions need an exact permission. Directly requested mutations
    that lack a supported exact tool/argument/target mapping remain denied;
    intent classification is not authorization. Unknown effect labels fail
    closed. Control tools must be explicitly identified by the dispatcher;
    this flag is never inferred from the tool name here.
    """

    if contract.required_tools == _FINANCIAL_REVIEW_TOOLS:
        if control_tool:
            return tool_name in {"task_plan", "task_list", "end_turn", "enable_reasoning"}
        if tool_name not in _FINANCIAL_REVIEW_TOOLS:
            return False
    if tool_name == "delegate_task":
        return control_tool and contract.mode in {
            "execute", "investigate_report", "plan", "continue"
        }
    if control_tool:
        return True
    effect = str(side_effect or "").strip().casefold()
    if effect in {"read", "read_only", "none"}:
        return True
    if effect not in {"mutation", "mutate", "conditional", "external"}:
        return False
    return mutation_allowed(contract, tool_name, arguments)


def next_required_tool(
    contract: AutonomyContract,
    completed_tools: set[str] | frozenset[str],
) -> str | None:
    """Return the next enforced tool in a deterministic contract workflow."""

    if contract.required_tools != _FINANCIAL_REVIEW_TOOLS:
        return None
    return next(
        (tool_name for tool_name in _FINANCIAL_REVIEW_ORDER if tool_name not in completed_tools),
        None,
    )


def is_bounded_financial_review(contract: AutonomyContract) -> bool:
    """Whether this contract uses the strict default finance workflow scope."""

    return isinstance(contract, AutonomyContract) and contract.required_tools == _FINANCIAL_REVIEW_TOOLS


__all__ = [
    "AutonomyContract",
    "IntentMode",
    "MutationPermission",
    "build_autonomy_contract",
    "is_bounded_financial_review",
    "mutation_allowed",
    "next_required_tool",
    "tool_call_allowed_by_contract",
]
