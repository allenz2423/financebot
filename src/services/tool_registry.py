"""A bounded registry for describing and dispatching Delilah tools.

The registry deliberately owns only tool metadata, schema exposure, argument
normalization, and result normalization.  It does not grant permissions or
replace the existing controller/router gates; callers can use the metadata
(``risk``, ``user_scope``, and ``side_effect``) when applying those policies.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping

from src.services.result_contracts import ResultContract
from src.services.tool_contract import validate_tool_arguments
from src.services.tool_execution_evidence import tool_result_indicates_failure
from src.services.tool_results import ToolResultEnvelope


ToolHandler = Callable[..., Any]


class ToolRegistryError(RuntimeError):
    """Base error for registry configuration and dispatch failures."""


class DuplicateToolError(ToolRegistryError, ValueError):
    """Raised when a tool name is registered more than once."""


class ToolCallNormalizationError(ToolRegistryError, ValueError):
    """Raised when a provider or caller supplies an unusable tool call."""


@dataclass(frozen=True)
class NormalizedToolCall:
    """Provider-independent representation of one tool call."""

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolDefinition:
    """Description and executable handler for one registered tool.

    ``schema`` is the JSON Schema for the function parameters.  The generated
    provider schema is available through :meth:`to_schema` and includes the
    Delilah metadata in an ``x-delilah`` extension field.
    """

    name: str
    handler: ToolHandler
    schema: Mapping[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    })
    description: str = ""
    toolset: str = "default"
    side_effect: str = "none"
    risk: str = "low"
    user_scope: str = "user"
    result_contract: ResultContract | Mapping[str, Any] | str | None = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("tool name must not be empty")
        object.__setattr__(self, "name", name)
        if not callable(self.handler):
            raise TypeError(f"handler for {name!r} must be callable")
        if not isinstance(self.schema, Mapping):
            raise TypeError(f"schema for {name!r} must be a mapping")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """Return a defensive copy of the parameter schema.

        Accepting a complete function schema as input is useful when migrating
        an existing catalog, while the normal form remains a JSON-Schema
        object containing ``type``/``properties``.
        """

        schema = copy.deepcopy(dict(self.schema))
        function = schema.get("function")
        if isinstance(function, Mapping):
            parameters = function.get("parameters")
            if isinstance(parameters, Mapping):
                return copy.deepcopy(dict(parameters))
        parameters = schema.get("parameters")
        if isinstance(parameters, Mapping) and schema.get("type") == "function":
            return copy.deepcopy(dict(parameters))
        return schema

    @property
    def contract_metadata(self) -> dict[str, Any]:
        contract = self.result_contract
        if isinstance(contract, ResultContract):
            return {
                "tool_name": contract.tool_name,
                "fact_keys": list(contract.fact_keys),
                "side_effect": contract.side_effect,
            }
        if isinstance(contract, Mapping):
            return copy.deepcopy(dict(contract))
        if contract is None:
            return {}
        return {"name": str(contract)}

    def to_schema(self) -> dict[str, Any]:
        """Return an OpenAI-compatible function tool schema."""

        result: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": str(self.name),
                "description": str(self.description or ""),
                "parameters": self.parameters_schema,
            },
            "x-delilah": {
                "toolset": str(self.toolset),
                "side_effect": str(self.side_effect),
                "risk": str(self.risk),
                "user_scope": str(self.user_scope),
                "result_contract": self.contract_metadata,
            },
        }
        return result

    # ``schema_for_model`` is a readable alias for callers building prompts.
    schema_for_model = to_schema


def normalize_tool_call(
    tool_or_call: str | Mapping[str, Any],
    arguments: Mapping[str, Any] | str | None = None,
    *,
    call_id: str | None = None,
) -> NormalizedToolCall:
    """Normalize direct and provider-shaped calls into one representation.

    Supported provider shape::

        {"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}

    Direct dispatch can use ``normalize_tool_call("lookup", {"key": "x"})``.
    Argument JSON must decode to an object; malformed input is rejected before
    a handler can run.
    """

    raw_name: Any = tool_or_call
    raw_arguments: Any = arguments
    raw_call_id: Any = call_id

    if isinstance(tool_or_call, Mapping):
        function = tool_or_call.get("function")
        if isinstance(function, Mapping):
            raw_name = function.get("name") or tool_or_call.get("name")
            if arguments is None:
                raw_arguments = function.get("arguments", tool_or_call.get("arguments"))
        else:
            raw_name = tool_or_call.get("name")
            if arguments is None:
                raw_arguments = tool_or_call.get("arguments")
        if call_id is None:
            raw_call_id = tool_or_call.get("id") or tool_or_call.get("call_id")

    name = str(raw_name or "").strip()
    if not name:
        raise ToolCallNormalizationError("tool call is missing a name")

    if raw_arguments in (None, ""):
        normalized_arguments: dict[str, Any] = {}
    elif isinstance(raw_arguments, Mapping):
        normalized_arguments = dict(raw_arguments)
    elif isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except (TypeError, ValueError) as exc:
            raise ToolCallNormalizationError(
                "tool arguments must be valid JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ToolCallNormalizationError("tool arguments JSON must be an object")
        normalized_arguments = dict(decoded)
    else:
        raise ToolCallNormalizationError("tool arguments must be a JSON object")

    normalized_call_id = str(raw_call_id or "").strip() or f"call_{uuid.uuid4().hex}"
    return NormalizedToolCall(name, normalized_arguments, normalized_call_id)


class ToolRegistry:
    """In-memory registry for a bounded set of callable tool definitions."""

    def __init__(self, definitions: Iterable[ToolDefinition] | None = None):
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions or ():
            self.register(definition)

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        if not isinstance(definition, ToolDefinition):
            raise TypeError("registry accepts ToolDefinition instances")
        name = str(definition.name).strip()
        if name in self._definitions:
            raise DuplicateToolError(f"tool already registered: {name}")
        self._definitions[name] = definition
        return definition

    def register_many(self, definitions: Iterable[ToolDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def get(self, name: str, default: ToolDefinition | None = None) -> ToolDefinition | None:
        return self._definitions.get(str(name).strip(), default)

    def lookup(self, name: str) -> ToolDefinition:
        """Look up a definition, raising ``KeyError`` when it is absent."""

        key = str(name).strip()
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {key}") from exc

    def definitions(
        self,
        *,
        toolset: str | None = None,
        user_scope: str | None = None,
    ) -> tuple[ToolDefinition, ...]:
        values = tuple(self._definitions.values())
        if toolset is not None:
            values = tuple(item for item in values if item.toolset == toolset)
        if user_scope is not None:
            values = tuple(item for item in values if item.user_scope == user_scope)
        return values

    def schemas(
        self,
        *,
        toolset: str | None = None,
        user_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """List defensive copies of model-facing schemas."""

        # Stable ordering keeps provider payloads, snapshots, and traces
        # reproducible even when definitions were registered by discovery.
        definitions = sorted(
            self.definitions(toolset=toolset, user_scope=user_scope),
            key=lambda item: item.name,
        )
        return [item.to_schema() for item in definitions]

    list_schemas = schemas

    def __contains__(self, name: object) -> bool:
        return str(name).strip() in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)

    def _prepare(
        self,
        tool_or_call: str | Mapping[str, Any],
        arguments: Mapping[str, Any] | str | None,
        call_id: str | None,
    ) -> tuple[NormalizedToolCall, ToolDefinition]:
        call = normalize_tool_call(tool_or_call, arguments, call_id=call_id)
        definition = self.lookup(call.name)
        validation_error = validate_tool_arguments(definition.to_schema(), call.arguments)
        if validation_error:
            raise ToolCallNormalizationError(validation_error)
        return call, definition

    def dispatch(
        self,
        tool_or_call: str | Mapping[str, Any],
        arguments: Mapping[str, Any] | str | None = None,
        *,
        call_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ToolResultEnvelope:
        """Dispatch a synchronous handler and normalize its result.

        Async handlers should be called through :meth:`dispatch_async`.  When
        called outside an event loop, this method also resolves an awaitable
        returned by a handler for convenience.
        """

        fallback_call_id = str(call_id or f"call_{uuid.uuid4().hex}")
        try:
            call, definition = self._prepare(tool_or_call, arguments, call_id)
        except (KeyError, ToolCallNormalizationError, ValueError, TypeError) as exc:
            return self._error_result(
                tool_name=self._best_effort_name(tool_or_call),
                call_id=fallback_call_id,
                receipt_id=receipt_id,
                error=str(exc),
            )

        try:
            raw_result = self._invoke(definition.handler, call.arguments)
            if inspect.isawaitable(raw_result):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    raw_result = asyncio.run(self._await_value(raw_result))
                else:
                    if inspect.iscoroutine(raw_result):
                        raw_result.close()
                    return self._error_result(
                        tool_name=definition.name,
                        call_id=call.call_id,
                        receipt_id=receipt_id,
                        side_effect=definition.side_effect,
                        error="async handler requires dispatch_async()",
                    )
            return self._normalize_result(
                definition,
                call,
                raw_result,
                receipt_id=receipt_id,
            )
        except Exception as exc:  # handler failures become bounded tool results
            return self._error_result(
                tool_name=definition.name,
                call_id=call.call_id,
                receipt_id=receipt_id,
                side_effect=definition.side_effect,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def dispatch_async(
        self,
        tool_or_call: str | Mapping[str, Any],
        arguments: Mapping[str, Any] | str | None = None,
        *,
        call_id: str | None = None,
        receipt_id: str | None = None,
    ) -> ToolResultEnvelope:
        """Async counterpart to :meth:`dispatch`, supporting async handlers."""

        fallback_call_id = str(call_id or f"call_{uuid.uuid4().hex}")
        try:
            call, definition = self._prepare(tool_or_call, arguments, call_id)
        except (KeyError, ToolCallNormalizationError, ValueError, TypeError) as exc:
            return self._error_result(
                tool_name=self._best_effort_name(tool_or_call),
                call_id=fallback_call_id,
                receipt_id=receipt_id,
                error=str(exc),
            )

        try:
            raw_result = self._invoke(definition.handler, call.arguments)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            return self._normalize_result(
                definition,
                call,
                raw_result,
                receipt_id=receipt_id,
            )
        except Exception as exc:  # handler failures become bounded tool results
            return self._error_result(
                tool_name=definition.name,
                call_id=call.call_id,
                receipt_id=receipt_id,
                side_effect=definition.side_effect,
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _invoke(handler: ToolHandler, arguments: dict[str, Any]) -> Any:
        """Call keyword-oriented handlers while accommodating mapping handlers."""

        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return handler(**arguments)
        parameters = tuple(signature.parameters.values())
        has_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        positional = tuple(
            parameter for parameter in parameters
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        )
        # Hermes' registry gives handlers one normalized argument mapping.  A
        # keyword-compatible handler remains convenient for Delilah's existing
        # small functions, so use that form when the callable advertises
        # **kwargs; otherwise pass the mapping as its single positional input.
        if len(positional) == 1 and not has_var_kwargs:
            return handler(arguments)
        return handler(**arguments)

    @staticmethod
    async def _await_value(value: Awaitable[Any]) -> Any:
        return await value

    @staticmethod
    def _best_effort_name(tool_or_call: str | Mapping[str, Any]) -> str:
        if isinstance(tool_or_call, str):
            return tool_or_call.strip() or "unknown_tool"
        function = tool_or_call.get("function")
        if isinstance(function, Mapping):
            return str(function.get("name") or tool_or_call.get("name") or "unknown_tool")
        return str(tool_or_call.get("name") or "unknown_tool")

    @staticmethod
    def _error_result(
        *,
        tool_name: str,
        call_id: str,
        receipt_id: str | None,
        error: str,
        side_effect: str = "none",
    ) -> ToolResultEnvelope:
        return ToolResultEnvelope.from_runtime(
            tool_name=tool_name,
            call_id=call_id,
            receipt_id=receipt_id,
            ok=False,
            complete=False,
            status="failed",
            summary="Tool execution failed.",
            error=error,
            side_effect=side_effect,
        )

    @classmethod
    def _normalize_result(
        cls,
        definition: ToolDefinition,
        call: NormalizedToolCall,
        raw_result: Any,
        *,
        receipt_id: str | None,
    ) -> ToolResultEnvelope:
        if isinstance(raw_result, ToolResultEnvelope):
            if tool_result_indicates_failure(raw_result):
                return cls._error_result(
                    tool_name=definition.name,
                    call_id=call.call_id,
                    receipt_id=receipt_id,
                    side_effect=definition.side_effect,
                    error=raw_result.error or raw_result.summary or "Tool execution failed.",
                )
            return replace(
                raw_result,
                tool_name=definition.name,
                call_id=call.call_id,
                receipt_id=receipt_id if receipt_id is not None else raw_result.receipt_id,
                # The registered definition is authoritative.  A handler must
                # not be able to relabel a mutation as a read by returning an
                # envelope with weaker metadata.
                side_effect=definition.side_effect,
            )

        explicit = raw_result if isinstance(raw_result, Mapping) else {}
        # Handler output is untrusted just like model output.  None, an empty
        # mapping, explicit error fields, and legacy textual error markers must
        # never be normalized into a confirmed success.
        # Numeric zero is a valid financial answer; use explicit failure
        # evidence instead of truthiness for scalar results.
        ok = raw_result is not None
        if isinstance(raw_result, Mapping):
            ok = bool(raw_result) and not tool_result_indicates_failure(raw_result)
        elif raw_result is not None:
            ok = not tool_result_indicates_failure(raw_result)
        if explicit.get("ok") is not None:
            ok = bool(explicit.get("ok")) and ok
        complete = bool(explicit.get("complete", ok))
        status = str(explicit.get("status") or ("confirmed" if ok else "failed"))
        summary = explicit.get("summary") or explicit.get("message")
        if not summary:
            summary = f"{definition.name} completed." if ok else "Tool execution failed."
        error = str(explicit.get("error") or "")

        explicit_facts = explicit.get("facts")
        facts = cls._extract_contract_facts(
            definition,
            explicit_facts if isinstance(explicit_facts, Mapping) else raw_result,
        )
        return ToolResultEnvelope.from_runtime(
            tool_name=definition.name,
            call_id=call.call_id,
            receipt_id=receipt_id,
            ok=ok,
            complete=complete,
            status=status,
            summary=str(summary),
            error=error,
            facts=facts,
            side_effect=definition.side_effect,
        )

    @staticmethod
    def _extract_contract_facts(
        definition: ToolDefinition,
        raw_result: Any,
    ) -> dict[str, Any]:
        contract = definition.result_contract
        if isinstance(contract, ResultContract):
            # Use the definition's local contract directly.  Requiring a
            # second global registration would make plugin/discovered tools
            # silently lose their declared facts.
            keys = contract.fact_keys
            if isinstance(raw_result, Mapping):
                return {
                    str(key): raw_result[key]
                    for key in keys
                    if key in raw_result
                    and isinstance(raw_result[key], (str, int, float, bool, type(None), list))
                }
            return {}
        if isinstance(contract, Mapping):
            keys = contract.get("fact_keys", ())
            if isinstance(keys, (list, tuple, set)) and isinstance(raw_result, Mapping):
                return {
                    str(key): raw_result[key]
                    for key in keys
                    if key in raw_result
                    and isinstance(raw_result[key], (str, int, float, bool, type(None), list))
                }
        return {}


__all__ = [
    "DuplicateToolError",
    "NormalizedToolCall",
    "ToolCallNormalizationError",
    "ToolDefinition",
    "ToolHandler",
    "ToolRegistry",
    "ToolRegistryError",
    "normalize_tool_call",
]
