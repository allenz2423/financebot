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

