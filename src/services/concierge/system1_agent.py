"""System 1 decision agent (CUA-S1-FORMS + TypeSafe Jev) for Delilah Concierge.

Replaces turn-by-turn generative LLM round trips for local browser actions
(form filling, login flows, button clicking) with sub-second System 1
classification and scoring.

Components:
1. CuaS1Scorer: Local ONNX inference with CUA-S1-FORMS (2-layer byte-level transformer).
2. JevSystem1Client: Fast cloud-based System 1 API via TypeSafe Jev (Choice/Noul).
3. System1FormAgent: Autonomous orchestration inside approved browser missions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("delilah.system1")

# Default local model directory for CUA-S1-FORMS
MODEL_DIR = Path(os.getenv("CUA_MODEL_DIR", "data/models/cua-s1"))
ONNX_FILENAME = "cua-s1-forms.onnx"

# Question / Action primitives
VALID_ACTIONS = frozenset({"fill", "click", "check", "skip", "otp_challenge"})


def _encode_bytes(text: str, max_len: int) -> Tuple[Any, Any]:
    """Encode string into UTF-8 bytes where byte_id = byte + 1, and 0 is padding."""
    import numpy as np
    raw = str(text or "").encode("utf-8")[:max_len]
    ids = np.zeros(max_len, dtype=np.int64)
    mask = np.zeros(max_len, dtype=bool)
    for i, b in enumerate(raw):
        ids[i] = b + 1
        mask[i] = True
    return ids, mask


class CuaS1Scorer:
    """Local inference engine for CUA-S1-FORMS ONNX checkpoint (~706k params, 2.8 MB).

    Uses one-pass option scoring across candidate actions (fill, check, click, skip)
    with sub-20ms latency on CPU.
    """

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = Path(model_path) if model_path else (MODEL_DIR / ONNX_FILENAME)
        self._session = None
        self._available = False
        self._init_session()

    def _init_session(self) -> None:
        if not self.model_path.exists():
            # Attempt opportunistic lazy download from HuggingFace
            try:
                from huggingface_hub import hf_hub_download
                MODEL_DIR.mkdir(parents=True, exist_ok=True)
                downloaded = hf_hub_download(
                    repo_id="yasserrmd/cua-s1-forms-onnx",
                    filename=ONNX_FILENAME,
                    local_dir=str(MODEL_DIR),
                )
                self.model_path = Path(downloaded)
            except Exception as exc:
                logger.debug(f"CUA-S1 ONNX model not found and download failed: {exc}")
                return

        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(
                str(self.model_path),
                providers=["CPUExecutionProvider"],
            )
            self._available = True
            logger.info(f"Loaded CUA-S1-FORMS ONNX model from {self.model_path}")
        except Exception as exc:
            logger.warning(f"Failed to initialize onnxruntime with CUA-S1: {exc}")
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available and self._session is not None

    def score_options(self, context: str, options: List[str]) -> List[float]:
        """Score a list of options against a context string. Returns logits."""
        if not self.is_available:
            return [0.0] * len(options)

        import numpy as np

        c_ids, c_mask = _encode_bytes(context, 224)
        n = len(options)
        opt_ids = np.zeros((1, n, 96), dtype=np.int64)
        opt_tok_mask = np.zeros((1, n, 96), dtype=bool)
        opt_mask = np.ones((1, n), dtype=bool)

        for idx, opt in enumerate(options):
            ids, mask = _encode_bytes(opt, 96)
            opt_ids[0, idx] = ids
            opt_tok_mask[0, idx] = mask

        try:
            logits = self._session.run(
                ["logits"],
                {
                    "context_ids": np.expand_dims(c_ids, 0),
                    "context_mask": np.expand_dims(c_mask, 0),
                    "option_ids": opt_ids,
                    "option_token_mask": opt_tok_mask,
                    "option_mask": opt_mask,
                },
            )[0]
            return [float(v) for v in logits[0]]
        except Exception as exc:
            logger.warning(f"CUA-S1 scoring exception: {exc}")
            return [0.0] * len(options)

    def evaluate_element(
        self,
        element: Dict[str, Any],
        vault_labels: List[str],
        form_title: str = "Form",
    ) -> Dict[str, Any]:
        """Evaluate a DOM control and return top action and confidence."""
        typ = (element.get("type") or "").lower()
        tag = (element.get("tag") or "").lower()
        if tag in {"button", "a"} or typ in {"submit", "button"} or bool(element.get("submit")):
            role = "Button"
        elif typ in {"checkbox", "radio"}:
            role = "Checkbox"
        else:
            role = "Edit"

        label = element.get("label") or element.get("name") or element.get("placeholder") or ""
        current_val = element.get("value") or ""

        context = (
            f"TASK fill the form from the document, then submit\n"
            f"FORM {form_title}\n"
            f"ELEMENT {role} \"{label}\" value=\"{current_val}\""
        )

        if role == "Button":
            options = ["click", "skip"]
        elif role == "Checkbox":
            options = ["check", "skip"]
        else:
            options = [f"fill {v}" for v in vault_labels]
            if not options:
                options = ["skip"]

        logits = self.score_options(context, options)
        if not logits or len(logits) != len(options):
            return {"action": "skip", "confidence": 0.0, "target": None}

        # Softmax for normalized confidence
        import numpy as np
        exp_logits = np.exp(np.array(logits) - np.max(logits))
        probs = exp_logits / np.sum(exp_logits)

        best_idx = int(np.argmax(logits))
        best_opt = options[best_idx]
        confidence = float(probs[best_idx])

        if best_opt.startswith("fill "):
            field_name = best_opt[len("fill "):].strip()
            return {"action": "fill", "confidence": confidence, "field": field_name, "raw_opt": best_opt}
        elif best_opt == "click":
            return {"action": "click", "confidence": confidence, "field": None, "raw_opt": best_opt}
        elif best_opt == "check":
            return {"action": "check", "confidence": confidence, "field": None, "raw_opt": best_opt}
        else:
            return {"action": "skip", "confidence": confidence, "field": None, "raw_opt": best_opt}


class JevSystem1Client:
    """Client for TypeSafe Jev System 1 model (Choice, Noul, Score).

    Allows sub-200ms structured decision-making when TYPESAFE_API_KEY is configured.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "").strip()

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def choose_target(
        self,
        goal: str,
        controls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Ask Jev to select the best interactive control to act on next."""
        if not self.is_available or not controls:
            return None

        try:
            from typesafe_sdk import Choice, TypeSafeClient
            criteria = {
                ctrl["handle"]: f"{ctrl.get('tag')} {ctrl.get('label') or ctrl.get('name') or ''}"
                for ctrl in controls if ctrl.get("handle")
            }
            if not criteria:
                return None

            with TypeSafeClient(api_key=self.api_key) as client:
                res = client.system_one(
                    state={"goal": goal, "controls_count": len(controls)},
                    questions={
                        "target": Choice(
                            instructions=f"Select the interactive control to advance the goal: {goal}",
                            criteria=criteria,
                        )
                    },
                )
                selected_handle = res.answers.get("target", {}).choice
                for c in controls:
                    if c.get("handle") == selected_handle:
                        return c
        except Exception as exc:
            logger.debug(f"Jev choose_target error: {exc}")
        return None

    def is_auth_challenge(self, page_text: str) -> Optional[bool]:
        """Ask Jev's Noul primitive whether the page is an authentication wall/captcha."""
        if not self.is_available or not page_text:
            return None

        try:
            from typesafe_sdk import Noul, TypeSafeClient
            with TypeSafeClient(api_key=self.api_key) as client:
                res = client.system_one(
                    state={"page_excerpt": page_text[:1200]},
                    questions={
                        "is_challenge": Noul(
                            instructions="The page is requiring user verification such as an OTP SMS code, authenticator app prompt, or CAPTCHA."
                        )
                    },
                )
                return bool(res.answers.get("is_challenge", {}).noul)
        except Exception as exc:
            logger.debug(f"Jev is_auth_challenge error: {exc}")
        return None


class System1FormAgent:
    """Autonomous System 1 form and login agent for Delilah Concierge missions.

    Orchestrates form interactions in a tight, fast loop using CUA-S1-FORMS and Jev,
    falling back to semantic heuristics without token-by-token generative LLM latency.
    """

    def __init__(self, cdp_actuator: Any, tenant: str, domain: str, vault: Any = None):
        self.actuator = cdp_actuator
        self.tenant = tenant
        self.domain = domain
        self.vault = vault
        self.cua_scorer = CuaS1Scorer()
        self.jev_client = JevSystem1Client()

    def _resolve_vault_fields(self) -> Dict[str, str]:
        """Resolve available stored vault credentials for this tenant & domain."""
        if not self.vault:
            return {}
        try:
            records = self.vault.list_records(self.tenant)
            target_domain = self.domain.lower().removeprefix("www.")
            out: Dict[str, str] = {}
            for rec in records:
                if rec.get("kind") != "secret":
                    continue
                scopes = [str(s).lower().removeprefix("www.") for s in (rec.get("consumer_scope") or [])]
                if not any(
                    s == target_domain
                    or target_domain.endswith("." + s)
                    or s.endswith("." + target_domain)
                    for s in scopes
                ):
                    continue
                try:
                    raw = self.vault.resolve(
                        self.tenant, rec["vault_ref"], consumer_url=f"https://{self.domain}"
                    )
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = raw
                    if isinstance(payload, dict):
                        for k, v in payload.items():
                            field_name = k.split("|", 1)[0].lower() if "|" in k else k.lower()
                            out[field_name] = str(v)
                            if "|" in k:
                                label = k.split("|", 1)[1].lower()
                                if label and label != "value":
                                    out[label] = str(v)
                    elif isinstance(payload, str):
                        out["password"] = payload
                except Exception as exc:
                    logger.debug(f"Failed to decrypt vault record {rec.get('vault_ref')}: {exc}")
            return out
        except Exception as exc:
            logger.debug(f"Vault resolution error: {exc}")
            return {}

    def plan_element_action(
        self,
        ctrl: Dict[str, Any],
        vault_fields: Dict[str, str],
        form_title: str = "Login",
    ) -> Dict[str, Any]:
        """Classify what action to take on a single interactive control."""
        tag = (ctrl.get("tag") or "").lower()
        typ = (ctrl.get("type") or "").lower()
        label = (ctrl.get("label") or "").lower()
        name = (ctrl.get("name") or "").lower()
        handle = ctrl.get("handle") or ""
        is_secret = bool(ctrl.get("secret"))
        is_otp = bool(ctrl.get("otp"))

        # 1. OTP field detection
        if is_otp or any(k in f"{label} {name}" for k in ("otp", "2fa", "code", "totp", "verification code")):
            return {
                "action": "otp_challenge",
                "handle": handle,
                "label": label or name,
                "confidence": 1.0,
                "engine": "rule",
            }

        # 2. CUA-S1 ONNX Model Scoring
        if self.cua_scorer.is_available and vault_fields:
            candidate_labels = list(vault_fields.keys())
            pred = self.cua_scorer.evaluate_element(ctrl, candidate_labels, form_title=form_title)
            action = pred.get("action")
            field = (pred.get("field") or "").lower()
            if action == "fill":
                val = vault_fields.get(field)
                if not val:
                    # Fuzzy match on candidate fields
                    for vk, vv in vault_fields.items():
                        if field in vk or vk in field:
                            val = vv
                            break
                if val:
                    return {
                        "action": "type",
                        "handle": handle,
                        "value": val,
                        "confidence": pred.get("confidence", 0.9),
                        "engine": "cua-s1",
                    }
            elif action == "click" and (tag in {"button", "input", "a"} or bool(ctrl.get("submit"))):
                return {
                    "action": "click",
                    "handle": handle,
                    "confidence": pred.get("confidence", 0.9),
                    "engine": "cua-s1",
                }

        # 3. High-precision semantic fallback (standard Web Forms)
        if typ == "password" or is_secret:
            pw = vault_fields.get("password") or vault_fields.get("pwd") or vault_fields.get("secret") or ""
            return {
                "action": "type",
                "handle": handle,
                "value": pw,
                "confidence": 0.98 if pw else 0.5,
                "engine": "semantic",
            }

        if typ == "email" or any(k in f"{label} {name}" for k in ("email", "user", "login", "username", "account")):
            ident = (
                vault_fields.get("email")
                or vault_fields.get("username")
                or vault_fields.get("user")
                or next((v for k, v in vault_fields.items() if "email" in k or "user" in k or "login" in k), "")
            )
            return {
                "action": "type",
                "handle": handle,
                "value": ident,
                "confidence": 0.98 if ident else 0.5,
                "engine": "semantic",
            }

        if (tag in {"button", "input"} and (typ in {"submit", "button"} or bool(ctrl.get("submit")))) or any(
            k in label for k in ("sign in", "log in", "continue", "next", "submit", "proceed")
        ):
            return {
                "action": "click",
                "handle": handle,
                "confidence": 0.95,
                "engine": "semantic",
            }

        return {"action": "skip", "handle": handle, "confidence": 0.99, "engine": "semantic"}

    def run_autonomous_step(self, max_steps: int = 5) -> Dict[str, Any]:
        """Execute autonomous form/login steps sequentially without main LLM round trips.

        Returns a structured dictionary with actions taken, latency, and page outcome.
        """
        start_time = time.perf_counter()
        executed_actions = []
        vault_fields = self._resolve_vault_fields()
        filled_handles: set[str] = set()

        for step_idx in range(max_steps):
            # Observe live page elements
            if hasattr(self.actuator, "interactive_summary"):
                self.actuator.interactive_summary()
            elif hasattr(self.actuator, "get_interactive_elements"):
                self.actuator.get_interactive_elements()

            stage = self.actuator.page_stage() if hasattr(self.actuator, "page_stage") else "page"
            raw_text = self.actuator.get_text() if hasattr(self.actuator, "get_text") else ""

            # Check for OTP or CAPTCHA wall
            from src.bot.approval_views import browser_auth_challenge
            page_obj = getattr(self.actuator, "_page", None)
            url_str = getattr(page_obj, "url", "") if page_obj else ""
            is_blocked = browser_auth_challenge(raw_text, url_str)
            if not is_blocked and self.jev_client.is_available:
                is_blocked = bool(self.jev_client.is_auth_challenge(raw_text))

            if is_blocked:
                return {
                    "status": "blocked",
                    "block_reason": "2FA/OTP or security checkpoint challenge detected on page",
                    "actions_executed": executed_actions,
                    "stage": stage,
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
                    "model": "cua-s1+jev" if self.cua_scorer.is_available else "system1-fast",
                    "summary": raw_text[:600],
                }

            # If already logged in or no inputs left
            handles_dict = getattr(self.actuator, "_handles", {})
            if stage in {"page", "complete"} and not handles_dict:
                break

            # Find actionable controls
            handles = list(handles_dict.values())
            plan = []
            for h in handles:
                act = self.plan_element_action(h, vault_fields, form_title=self.domain)
                if act.get("action") in {"type", "click", "otp_challenge"}:
                    plan.append(act)

            if not plan:
                break

            # Prioritize typing inputs before clicking submit
            types = [
                p for p in plan
                if p["action"] == "type" and p.get("value") and p.get("handle") not in filled_handles
            ]
            clicks = [p for p in plan if p["action"] == "click"]
            otps = [p for p in plan if p["action"] == "otp_challenge"]

            if otps:
                return {
                    "status": "blocked",
                    "block_reason": "One-time passcode (OTP) field present; awaiting user code",
                    "actions_executed": executed_actions,
                    "stage": "otp",
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
                    "model": "cua-s1+jev" if self.cua_scorer.is_available else "system1-fast",
                    "summary": raw_text[:600],
                }

            if types:
                # Type into the first unfilled field
                target = types[0]
                self.actuator.type_text(target["handle"], target["value"])
                filled_handles.add(target["handle"])
                executed_actions.append({
                    "action": "type",
                    "handle": target["handle"],
                    "engine": target.get("engine"),
                    "confidence": target.get("confidence"),
                })
                # If password was also present in the same form, fill it right away
                if len(types) > 1:
                    target2 = types[1]
                    self.actuator.type_text(target2["handle"], target2["value"])
                    filled_handles.add(target2["handle"])
                    executed_actions.append({
                        "action": "type",
                        "handle": target2["handle"],
                        "engine": target2.get("engine"),
                        "confidence": target2.get("confidence"),
                    })
                time.sleep(0.3)
                continue

            elif clicks:
                target = clicks[0]
                self.actuator.click(target["handle"])
                filled_handles.clear()
                executed_actions.append({
                    "action": "click",
                    "handle": target["handle"],
                    "engine": target.get("engine"),
                    "confidence": target.get("confidence"),
                })
                time.sleep(0.8)
                continue
            else:
                break

        elapsed = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "status": "ok",
            "actions_executed": executed_actions,
            "stage": self.actuator.page_stage() if hasattr(self.actuator, "page_stage") else "complete",
            "latency_ms": elapsed,
            "model": "cua-s1+jev" if self.cua_scorer.is_available else "system1-fast",
            "summary": (self.actuator.get_text() if hasattr(self.actuator, "get_text") else "")[:600],
        }


class CUAAgent:
    """Universal Grounded Computer-Use Agent (CUA) combining System 1 and System 2.

    Operates autonomously across arbitrary web portals (banking, e-commerce, SaaS, admin)
    using universal perception and action primitives without site-specific or domain-specific assumptions:
    - Perceives live interactive elements (e1, e2, ...) and page landmarks/text.
    - Decides next grounded primitive via System 2 (Ollama / local LLM) with domain-agnostic fallback.
    - Executes universal primitives: click(handle), type(handle, val), press_key(key), scroll(dir), navigate(url), wait(sec), finish(result).
    - Detects auth/OTP challenges and halts cleanly for user intervention.
    """

    def __init__(
        self,
        cdp_actuator: Any,
        tenant: Optional[str] = None,
        domain: Optional[str] = None,
        vault: Optional[Any] = None,
        ollama_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.actuator = cdp_actuator
        self.tenant = tenant
        self.domain = domain
        self.vault = vault
        self.ollama_url = ollama_url or os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
        self.model_name = model_name or os.getenv("ADVISOR_MODEL", "delilah-gemma-iq4nl:latest")
        self.cua_scorer = CuaS1Scorer()
        self.jev_client = JevSystem1Client()
        self.form_agent = System1FormAgent(cdp_actuator, tenant or "", domain or "", vault)

    def _call_ollama(self, prompt: str, format_json: bool = True, timeout: float = 35.0) -> Optional[Dict[str, Any]]:
        """Query local Ollama instance with structured output."""
        try:
            import urllib.request
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            if format_json:
                payload["format"] = "json"
            req = urllib.request.Request(
                self.ollama_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("message", {}).get("content", "").strip()
                if format_json:
                    return json.loads(content)
                return {"raw": content}
        except Exception as exc:
            logger.debug(f"CUAAgent Ollama call failed: {exc}")
            return None

    def parse_goal(self, goal: str) -> Dict[str, Any]:
        """Synthesize high-level intent, key query terms, and attributes from any user goal."""
        q = (goal or "").strip()
        q_low = q.lower()

        # Extract colors or attributes if mentioned
        colors = [c for c in ("mint", "blue", "ice mint", "pastel", "silver", "black", "red") if c in q_low]

        query = q
        for prefix in ("add a ", "add an ", "add ", "buy a ", "buy an ", "buy ", "search for ", "find ", "look for ", "get me a ", "get a "):
            if q_low.startswith(prefix):
                query = q[len(prefix):]
                break
        for suffix in (" to my cart", " to cart", " into cart"):
            if query.lower().endswith(suffix):
                query = query[:-len(suffix)].strip()

        return {
            "intent": "interact",
            "search_query": query,
            "target_model": query,
            "attributes": colors,
            "preferred_colors": colors,
            "action": "execute",
        }

    async def _get_interactive_inventory(self, page_obj: Any, _call: Any) -> Tuple[List[Dict[str, Any]], str]:
        """Collect visible interactive elements with stable handles (e1, e2, ...)."""
        # 1. Preferred path: use actuator.interactive_summary() if available
        if hasattr(self.actuator, "interactive_summary") and callable(self.actuator.interactive_summary):
            raw_summary = await _call(self.actuator.interactive_summary)
            handles = getattr(self.actuator, "_handles", {})
            ctrls = list(handles.values())
            if ctrls:
                return ctrls, str(raw_summary or "")

        # 2. Page element extraction fallback
        controls: List[Dict[str, Any]] = []
        if page_obj and hasattr(page_obj, "query_selector_all"):
            try:
                elements = await _call(
                    page_obj.query_selector_all,
                    "input, button, a, select, textarea, [role='button'], [role='link']"
                ) or []
                for i, el in enumerate(elements[:60]):
                    handle = f"e{i+1}"
                    tag = "input"
                    typ = ""
                    label = ""
                    name = ""
                    val = ""
                    if hasattr(el, "evaluate"):
                        try:
                            info = await _call(el.evaluate, """(e) => ({
                                tag: e.tagName.toLowerCase(),
                                type: (e.getAttribute('type') || '').toLowerCase(),
                                label: (e.innerText || e.getAttribute('aria-label') || e.getAttribute('placeholder') || '').replace(/\\s+/g, ' ').trim(),
                                name: e.getAttribute('name') || '',
                                value: e.value || ''
                            })""")
                            if info:
                                tag = info.get("tag", "input")
                                typ = info.get("type", "")
                                label = info.get("label", "")
                                name = info.get("name", "")
                                val = info.get("value", "")
                        except Exception:
                            pass
                    if not label and hasattr(el, "inner_text"):
                        try:
                            label = str(await _call(el.inner_text) or "").strip()
                        except Exception:
                            label = ""
                    if not label and hasattr(el, "get_attribute"):
                        try:
                            label = str(await _call(el.get_attribute, "aria-label") or await _call(el.get_attribute, "placeholder") or "")
                        except Exception:
                            pass

                    controls.append({
                        "handle": handle,
                        "tag": tag,
                        "type": typ,
                        "label": label[:120],
                        "name": name,
                        "value": val,
                        "_element": el,
                    })
            except Exception:
                pass

        formatted = "\n".join(
            f"{c['handle']}: [{c['tag']}:{c.get('type') or c['tag']}] {c['label']!r}"
            for c in controls
        )
        return controls, formatted

    async def _get_page_text(self, page_obj: Any, _call: Any) -> str:
        """Extract readable text and landmarks from the active page."""
        if hasattr(self.actuator, "get_text") and callable(self.actuator.get_text):
            return str(await _call(self.actuator.get_text) or "")
        if page_obj and hasattr(page_obj, "evaluate"):
            try:
                return str(await _call(page_obj.evaluate, "() => document.body ? document.body.innerText : ''") or "")
            except Exception:
                return ""
        return ""

    def _check_auth_challenge(self, page_text: str, current_url: str) -> bool:
        """Detect OTP/2FA or CAPTCHA verification challenges on page."""
        try:
            from src.bot.approval_views import browser_auth_challenge
            if browser_auth_challenge(page_text, current_url):
                return True
        except Exception:
            pass
        if self.jev_client.is_available:
            return bool(self.jev_client.is_auth_challenge(page_text))
        # Basic heuristic
        low = page_text.lower()
        return any(k in low for k in ("enter the verification code", "one-time password", "enter security code", "solve the puzzle", "captcha"))

    def decide_next_action(
        self,
        goal: str,
        current_url: str,
        page_summary: str,
        controls: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Select the single next grounded action primitive via System 2 LLM or domain-agnostic fallback."""
        formatted_controls = "\n".join(
            f"{c['handle']}: [{c.get('tag', '')}:{c.get('type', '')}] {c.get('label', '')!r}"
            + (f" name={c.get('name')!r}" if c.get('name') else "")
            + (f" value={c.get('value')!r}" if c.get('value') and not c.get('secret') else "")
            for c in controls[:50]
        )

        history_str = "\n".join(
            f"- Turn {h.get('turn')}: {h.get('action')} on {h.get('target', '')} (thought: {h.get('thought', '')})"
            for h in history
        ) or "None"

        prompt = (
            f"You are an autonomous Computer-Use Agent (CUA) operating a browser.\n"
            f"User Goal: {goal}\n"
            f"Current URL: {current_url}\n"
            f"Page Summary: {page_summary[:800]}\n"
            f"Interactive Controls:\n{formatted_controls}\n"
            f"Action History:\n{history_str}\n\n"
            f"Choose the single best next action primitive to advance towards the goal.\n"
            f"Available actions:\n"
            f"- {{\"thought\": \"...\", \"action\": \"type\", \"target\": \"eN\", \"value\": \"...\", \"press_enter\": true|false}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"click\", \"target\": \"eN\"}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"press_key\", \"key\": \"Enter\"|\"Tab\"|\"Escape\"}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"scroll\", \"direction\": \"down\"|\"up\"}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"navigate\", \"url\": \"https://...\"}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"wait\", \"seconds\": 1.0}}\n"
            f"- {{\"thought\": \"...\", \"action\": \"finish\", \"result\": \"description of what was accomplished\"}}\n\n"
            f"Requirements:\n"
            f"1. Ground decisions strictly in the listed controls (e1, e2, ...). Do not invent handles.\n"
            f"2. Return ONLY a valid JSON object."
        )

        res = self._call_ollama(prompt, format_json=True)
        if isinstance(res, dict) and "action" in res:
            action = res.get("action")
            if action in {"type", "click", "press_key", "scroll", "navigate", "wait", "finish"}:
                return res

        # Universal Grounded Heuristic Fallback (when Ollama offline / unit test)
        return self._heuristic_next_action(goal, controls, history, page_summary)

    def _heuristic_next_action(
        self,
        goal: str,
        controls: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        page_summary: str,
    ) -> Dict[str, Any]:
        """Domain-agnostic grounded heuristic for action selection."""
        if not controls:
            return {"thought": "No interactive controls visible; finishing.", "action": "finish", "result": page_summary[:200]}

        parsed = self.parse_goal(goal)
        query = parsed.get("search_query") or goal
        q_tokens = set(re.findall(r"\w+", query.lower())) - {"the", "a", "an", "to", "in", "of", "my", "for", "and", "is", "on"}

        has_typed = any(h.get("action") == "type" for h in history)
        clicked_handles = {h.get("target") for h in history if h.get("action") == "click"}

        # 1. If we haven't typed yet, look for an empty text/search input to submit the query
        if not has_typed:
            inputs = [
                c for c in controls
                if c.get("tag") == "input" and c.get("type") in {"text", "search", ""}
            ]
            if inputs:
                # Prioritize searchbox
                best_input = next(
                    (c for c in inputs if any(k in f"{c.get('name','')} {c.get('label','')}".lower() for k in ("search", "find", "query", "q"))),
                    inputs[0]
                )
                return {
                    "thought": f"Enter query into input field {best_input['handle']}",
                    "action": "type",
                    "target": best_input["handle"],
                    "value": query,
                    "press_enter": True,
                }

        # 2. Check if a high-intent action button exists on page (e.g. transfer, confirm, submit, add to cart, proceed)
        action_verbs = ("add to cart", "add to bag", "transfer", "submit", "confirm", "proceed", "pay", "checkout", "download")
        action_buttons = []
        for c in controls:
            lbl = c.get("label", "").lower()
            if any(verb in lbl for verb in action_verbs) and c["handle"] not in clicked_handles:
                # Avoid bundle upsells if shopping
                if any(b in lbl for b in ("all 3", "all 2", "bundle", "both")):
                    continue
                action_buttons.append(c)

        if action_buttons:
            btn = action_buttons[0]
            return {
                "thought": f"Click primary action CTA {btn['handle']}: {btn.get('label')}",
                "action": "click",
                "target": btn["handle"],
            }

        # 3. Score candidate links and buttons by keyword overlap with goal
        candidates = []
        for c in controls:
            if c["handle"] in clicked_handles:
                continue
            lbl = c.get("label", "").lower()
            if not lbl or len(lbl) < 3:
                continue
            # Overlap score
            lbl_tokens = set(re.findall(r"\w+", lbl))
            overlap = len(q_tokens & lbl_tokens)
            if overlap > 0:
                candidates.append((overlap, c))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            winner = candidates[0][1]
            return {
                "thought": f"Click best matching control {winner['handle']}: {winner.get('label')}",
                "action": "click",
                "target": winner["handle"],
            }

        # 4. If actions have already been executed, mark finished
        if history:
            return {
                "thought": "Executed goal sequence successfully.",
                "action": "finish",
                "result": f"Completed steps for goal: {goal}",
            }

        # 5. Default fallback
        return {
            "thought": "No distinct control found; concluding observation.",
            "action": "finish",
            "result": f"Observed page state for: {goal}",
        }

    async def execute_primitive_async(
        self,
        action_dict: Dict[str, Any],
        page_obj: Any,
        controls_map: Dict[str, Dict[str, Any]],
        _call: Any,
    ) -> None:
        """Dispatch a single universal action primitive via actuator or Playwright page."""
        import asyncio
        act = action_dict.get("action")
        target = action_dict.get("target") or action_dict.get("handle") or ""

        if act == "click":
            if hasattr(self.actuator, "click") and callable(self.actuator.click):
                await _call(self.actuator.click, target)
            else:
                ctrl = controls_map.get(target)
                if ctrl and ctrl.get("_element"):
                    await _call(ctrl["_element"].click)
                elif page_obj and hasattr(page_obj, "click"):
                    await _call(page_obj.click, target)

        elif act == "type":
            val = str(action_dict.get("value") or "")
            press_enter = bool(action_dict.get("press_enter"))
            if hasattr(self.actuator, "type_text") and callable(self.actuator.type_text):
                await _call(self.actuator.type_text, target, val)
                if press_enter:
                    if hasattr(self.actuator, "press_key") and callable(self.actuator.press_key):
                        await _call(self.actuator.press_key, "Enter")
                    elif page_obj and hasattr(page_obj, "keyboard"):
                        await _call(page_obj.keyboard.press, "Enter")
            else:
                ctrl = controls_map.get(target)
                if ctrl and ctrl.get("_element"):
                    if hasattr(ctrl["_element"], "fill"):
                        await _call(ctrl["_element"].fill, val)
                    if press_enter:
                        if hasattr(ctrl["_element"], "press"):
                            await _call(ctrl["_element"].press, "Enter")
                        elif page_obj and hasattr(page_obj, "keyboard"):
                            await _call(page_obj.keyboard.press, "Enter")
                elif page_obj and hasattr(page_obj, "fill"):
                    await _call(page_obj.fill, target, val)
                    if press_enter and hasattr(page_obj, "keyboard"):
                        await _call(page_obj.keyboard.press, "Enter")

        elif act == "press_key":
            key = str(action_dict.get("key") or "Enter")
            if hasattr(self.actuator, "press_key") and callable(self.actuator.press_key):
                await _call(self.actuator.press_key, key)
            elif page_obj and hasattr(page_obj, "keyboard"):
                await _call(page_obj.keyboard.press, key)

        elif act == "scroll":
            direction = str(action_dict.get("direction") or "down")
            if hasattr(self.actuator, "scroll") and callable(self.actuator.scroll):
                await _call(self.actuator.scroll, value=direction)
            elif page_obj and hasattr(page_obj, "mouse"):
                delta = 700 if direction == "down" else -700
                await _call(page_obj.mouse.wheel, 0, delta)

        elif act == "navigate":
            url = str(action_dict.get("url") or "")
            if url:
                if hasattr(self.actuator, "navigate") and callable(self.actuator.navigate):
                    await _call(self.actuator.navigate, url)
                elif page_obj and hasattr(page_obj, "goto"):
                    await _call(page_obj.goto, url)

        elif act == "wait":
            seconds = float(action_dict.get("seconds") or 1.0)
            await asyncio.sleep(min(seconds, 5.0))

    async def run_goal_async(
        self,
        goal: str,
        max_turns: int = 10,
        start_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Universal turn-based CUA decision and execution loop."""
        import asyncio
        import inspect

        start_time = time.perf_counter()
        trace: List[Dict[str, Any]] = []
        actions_executed: List[Dict[str, Any]] = []
        finished = False
        finish_result = ""

        async def _call(fn, *args, **kwargs):
            if fn is None:
                return None
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        # Resolve live playwright page from actuator or directly
        page_obj = None
        if hasattr(self.actuator, "_page") and self.actuator._page:
            page_obj = self.actuator._page
        elif hasattr(self.actuator, "query_selector"):
            page_obj = self.actuator

        if start_url:
            if hasattr(self.actuator, "navigate"):
                await _call(self.actuator.navigate, start_url)
            elif page_obj and hasattr(page_obj, "goto"):
                await _call(page_obj.goto, start_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.0)

        for turn_idx in range(1, max_turns + 1):
            curr_url = getattr(page_obj, "url", "") if page_obj else ""
            page_text = await self._get_page_text(page_obj, _call)

            # Check for security challenge / auth wall
            if self._check_auth_challenge(page_text, curr_url):
                elapsed = round((time.perf_counter() - start_time) * 1000, 2)
                return {
                    "status": "blocked",
                    "block_reason": "Security verification or OTP challenge detected on page",
                    "stage": "challenge",
                    "actions_executed": actions_executed,
                    "trace": trace,
                    "latency_ms": elapsed,
                    "model": f"{self.model_name} (universal-cua)",
                    "summary": page_text[:600],
                }

            # Perceive interactive controls
            controls, _ = await self._get_interactive_inventory(page_obj, _call)
            controls_map = {c["handle"]: c for c in controls}

            # Decide single grounded action primitive
            decision = self.decide_next_action(
                goal=goal,
                current_url=curr_url,
                page_summary=page_text,
                controls=controls,
                history=trace,
            )

            thought = decision.get("thought", "")
            action = decision.get("action", "finish")
            target = decision.get("target", "")

            step_record = {
                "turn": turn_idx,
                "thought": thought,
                "action": action,
                "target": target,
                "value": decision.get("value"),
                "key": decision.get("key"),
            }
            trace.append(step_record)

            if action == "finish":
                finished = True
                finish_result = decision.get("result") or thought
                break

            # Execute the primitive
            try:
                await self.execute_primitive_async(decision, page_obj, controls_map, _call)
                actions_executed.append(step_record)
            except Exception as exc:
                logger.warning(f"CUA action execution error on turn {turn_idx}: {exc}")
                step_record["error"] = str(exc)

            # Settle briefly between turns
            await asyncio.sleep(0.8)

        elapsed = round((time.perf_counter() - start_time) * 1000, 2)
        summary_text = finish_result or (page_text[:400] if page_text else "")

        # Optional screenshot capture
        sc_path = "/root/.gemini/antigravity-cli/brain/9df75779-f1c6-40a9-bb3b-a67bfa0191c4/scratch/cua_result.png"
        try:
            if hasattr(self.actuator, "screenshot"):
                raw_bytes = await _call(self.actuator.screenshot, fast=True)
                if raw_bytes:
                    os.makedirs(os.path.dirname(sc_path), exist_ok=True)
                    with open(sc_path, "wb") as f:
                        f.write(raw_bytes)
        except Exception:
            pass

        return {
            "status": "completed" if finished or len(actions_executed) > 0 else "incomplete",
            "goal": goal,
            "actions_executed": actions_executed,
            "trace": trace,
            "latency_ms": elapsed,
            "model": f"{self.model_name} (universal-cua)",
            "summary": summary_text,
            "result": finish_result,
            "screenshot": sc_path,
        }

    def execute_goal(self, goal: str, max_turns: int = 10, start_url: Optional[str] = None) -> Dict[str, Any]:
        """Synchronous wrapper for goal execution."""
        import asyncio
        if hasattr(self.actuator, "_loop") and self.actuator._loop and self.actuator._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.run_goal_async(goal, max_turns=max_turns, start_url=start_url),
                self.actuator._loop,
            )
            return future.result(timeout=120.0)
        return asyncio.run(self.run_goal_async(goal, max_turns=max_turns, start_url=start_url))

