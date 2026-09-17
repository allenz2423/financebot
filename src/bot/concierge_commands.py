"""!concierge admin command plane (B0.

Deterministic prefix commands, admins from config ONLY (CONCIERGE_ADMINS),
tenant from the @mention (interaction-derived,, least-privilege enable
(read tier,, $0 cap),, every action audited.  Non-admins are refused AND the
refusal is audited (attempts are worth recording).

    !concierge enable  @user [read|write|spend]
    !concierge disable @user
    !concierge tier    @user <read|write|spend>
    !concierge limit   @user <cap-usd>
    !concierge allow   @user <domain>
    !concierge status  [@user]
    !concierge kill    <draft_id>          (deny an in-flight draft)
    !concierge audit   [@user] [n]         (tail the audit chain)

Wiring: imported for side effects by ``src.bot.commands`` so the command
registers on the shared bot; services default to data/concierge.db.


"""

from __future__ import annotations

import json
import re
import shlex

from src.core.state import bot
from src.security.vault import DEFAULT_DB_PATH as _DB
from src.services.concierge.audit import AuditLog
from src.services.concierge.state import DraftStore
from src.services.concierge.tenants import NotAdminError, TenantError, TenantStore

AUDIT = AuditLog(_DB)
TENANTS = TenantStore(_DB)
DRAFTS = DraftStore(_DB)


def _actor(ctx) -> str:
    return "user:" + str(ctx.author.id)


def _tenant_of(ctx):
    """Tenant identity is interaction-derived: the explicit @mention."""
    if ctx.message.mentions:
        return "user:" + str(ctx.message.mentions[0].id)
    return None


def _parse_mention(args) -> Optional[str]:
    for arg in args:
        m = re.match(r"^<@!?([0-9]+)>$", arg)
        if m:
            return "user:" + m.group(1)
    return None


async def _deny(ctx, reason: str) -> None:
    AUDIT.append(actor=_actor(ctx), action="admin_denied", detail={"reason": reason})
    await ctx.send("Access denied: " + reason)


@bot.command(name="concierge")
async def concierge_admin(ctx, *, raw: str = ""):
    try:
        args = shlex.split(raw or "")
    except ValueError:
        args = (raw or "").split()

    if not args:
        await ctx.send(
            "Usage: `!concierge enable|disable|tier|limit|allow|status|kill|audit` (admins only)"
        )
        return

    action = args[0].lower()
    tenant = _tenant_of(ctx) if action != "audit" else _parse_mention(args[1:])
    actor = _actor(ctx)

    try:
        if action == "enable":
            if tenant is None:
                return await _deny(ctx, "enable requires an @mention")
            tier = "read"
            if len(args) > 2 and args[2].lower() in ("read", "write", "spend"):
                tier = args[2].lower()
            TENANTS.enable(actor, tenant, tier=tier)

            AUDIT.append(actor=actor, action="admin_enable", tenant=tenant,
                         detail={"tier": tier, "spend_cap": 0.0})
            await ctx.send("Concierge enabled for " + tenant + " (" + tier + ", $0 cap).")
            return

        if action == "disable":
            if tenant is None:
                return await _deny(ctx, "disable requires an @mention")
            TENANTS.disable(actor, tenant)
            AUDIT.append(actor=actor, action="admin_disable", tenant=tenant)

            await ctx.send("Concierge disabled for " + tenant + ".")
            return

        if action == "tier":
            if tenant is None or len(args) < 3:
                return await _deny(ctx, "tier requires an @mention,and a tier")
            tier = args[2].lower()
            TENANTS.set_tier(actor, tenant, tier)
            AUDIT.append(actor=actor, action="admin_tier", tenant=tenant, detail={"tier": tier})
            await ctx.send("tier for " + tenant + " is now " + tier + ".")
            return

        if action == "limit":
            if tenant is None or len(args) < 3:
                return await _deny(ctx, "limit requires an @mention,and a cap")
            cap = float(args[2])
            TENANTS.set_spend_cap(actor, tenant, cap)
            AUDIT.append(actor=actor, action="admin_limit", tenant=tenant, detail={"cap": cap})
            await ctx.send("spend cap for " + tenant + " is now $" + f"{cap:.2f}" + ".")
            return

        if action == "allow":
            if tenant is None or len(args) < 3:
                return await _deny(ctx, "allow requires an @mention,and a domain")
            TENANTS.allow_domain(actor, tenant, args[2])
            AUDIT.append(actor=actor, action="admin_allow", tenant=tenant,
                         detail={"domain": args[2]})
            await ctx.send("allowed domain " + args[2] + " for " + tenant + ".")
            return

        if action == "status":
            target = tenant or ("user:" + str(ctx.author.id))
            AUDIT.append(actor=actor, action="admin_status", tenant=target)



            st = TENANTS.status(target)
            await ctx.send("`" + json.dumps(st, indent=2) + "`")
            return

        if action == "kill":
            if len(args) < 2:
                return await _deny(ctx, "kill requires a draft_id")
            draft_id = args[1]
            owner = DRAFTS.get(draft_id)["tenant"]
            DRAFTS.deny(draft_id, owner)
            AUDIT.append(actor=actor, action="admin_kill", tenant=owner,
                         detail={"draft_id": draft_id})
            await ctx.send("Draft " + draft_id + " denied.")
            return

        if action == "audit":
            n = 20
            try:
                n = min(int(args[-1]), 100)
            except (ValueError, IndexError):
                pass
            AUDIT.append(actor=actor, action="admin_audit", tenant=tenant)



            rows = AUDIT.tail(limit=n, tenant=tenant)
            text = "\n".join(
                f"`#{r['seq']}` {r['ts'][:19]} {r['actor']} {r['action']} {r['subject']}"
                for r in reversed(rows)
            ) or "(no audit rows)"
            await ctx.send(text[:1900])
            return

        await _deny(ctx, "unknown action " + action)
        return

    except NotAdminError:
        await _deny(ctx, "you are not a concierge admin")
        return
    except Exception as exc:
        msg = str(exc)[:300]
        AUDIT.append(actor=actor, action="admin_error", tenant=tenant, detail={"error": msg})
        await ctx.send("Error: " + msg)
        return


__all__ = ["concierge_admin", "AUDIT", "TENANTS", "DRAFTS"]