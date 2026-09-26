# Muse secure environment: research and Delilah design

Research checked 2026-09-26. This is a design translation, not a claim that
Meta's implementation is open source or independently audited here.

## What Meta publicly describes

Meta describes Muse as running in a dedicated persistent Linux VM with a
browser, storage, CPU, and memory for code, tools, subagents, and scheduled
work. Its safety write-up describes two security domains: the agent harness,
workspace, and tools run in a `systemd-nspawn` runtime container; security-
sensitive services remain outside that container. Container root maps to an
unprivileged host identity, with a separate root filesystem, virtual network,
filtered syscalls, and restricted capabilities.

Browser control is separated again: a broker mediates Chrome DevTools Protocol,
while a browser subagent sees an accessibility-tree snapshot instead of raw
DOM. The browser agent cannot execute page JavaScript or use DevTools. Meta
also describes pausing the agent during user takeover or credential entry,
classifiers for prompt injection/data exfiltration/high-risk forms, and human
approval for purchases. These are published architecture claims; they should
not be read as guarantees that prompt injection is solved.

Meta's developer computer-use API is distinct from Muse's hosted product: the
API defines an observe/act protocol, but the application author supplies the
desktop/VM, action driver, screenshot capture, validation, logging, and safety.
Meta's cookbook demonstrates a throwaway Linux desktop in a Docker container.
This supports the practical point that a useful “computer” abstraction need
not mean one heavyweight always-on hypervisor VM per user.

Sources:

- [Meta AI Research: How We Built Safety Into Muse](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse)
- [Meta Muse overview](https://ai.meta.com/muse/)
- [Meta Model API: computer-use agent](https://dev.meta.ai/docs/computer-use?project_id=1775916636764246&team_id=1331104075427093)
- [Meta cookbook: computer use with Muse Spark and a disposable Linux container](https://dev.meta.ai/docs/cookbook/computer-use?project_id=1082575054202150&team_id=2306091636825706)

## Delilah's current boundary

Delilah already has a shared Python/shell sandbox with non-root execution,
Landlock restrictions, resource limits, and disposable per-execution files.
It also has persistent per-user workspace and network access. It joins both
the internal Compose peer network and a public-egress network, so it can reach
internal peers as well as the public internet; this is not per-task egress
isolation. Its API uses a shared sandbox token plus a request-supplied user
identifier, not a host-bound task lease. The default output cap is extremely
large and output collection precedes truncation, so it is not a meaningful
per-task output bound. The Delilah container currently runs as root and has a
read/write finance-data mount. Several service ports are published without a
loopback bind. Those are concrete hardening concerns, but changing identity,
network membership, process supervision, or port exposure needs compatibility
and deployment review rather than a silent one-line change.

Browserless and the MCP browser are shared services, not task-owned browser
sessions. The bot was unnecessarily mounted to the host Docker socket. The only Docker CLI call
in bot runtime code was an optional Stirling-PDF IP-discovery fallback; this
change removes that fallback in favor of the already-configured service DNS
name, then removes the socket mount and Docker CLI package from the bot image.
Host-side setup scripts and evaluation tooling may still use Docker. No task
worker is exposed by this change. The sandbox's Compose comment was also
corrected so it no longer claims the internet network isolates internal peers.

The existing Stirling-PDF test checks configured-URL precedence and service-DNS
candidate selection only; Compose syntax validation does not prove live DNS or
network reachability, and no service was started for this review.

## Recommended target: a lightweight task computer

Use a fresh, task-owned container as the default execution unit, with the
option for operators to select a stronger microVM backend when their threat
model requires it. This offers an OS-like workspace and browser without
keeping a full VM running for every user. Do not call an ordinary container a
VM-grade security boundary: it shares the host kernel.

Required design constraints before enabling model-driven browser/computer use:

1. A narrow trusted worker manager owns container-runtime authority. The LLM,
   Delilah task code, browser process, and guest never receive a Docker socket
   or manager credential. The API accepts only fixed operations and
   server-constructed configuration; callers cannot choose image, mounts,
   host paths, network mode, capabilities, or arbitrary runtime options.
2. Owner and task identity are bound by the host-side task record, not guest
   arguments. Each task receives a separate writable scratch disk/workspace,
   operator-configurable CPU, memory, and wall-clock limits (with conservative
   bounded defaults), process/PID limits, and output bounds. Cleanup runs after
   success, failure, cancellation, timeout, and recovery; no shared mutable
   package installation is visible to later jobs.
3. Guest environment is allowlisted and contains no finance database, bot
   environment, Discord/Plaid/Gmail/model credentials, sandbox token, or
   manager credentials. Any required secret is injected by an executor-only
   broker for a narrowly scoped action and is not readable from model-visible
   page content or general shell execution.
4. Network is deny-by-default. If web access is enabled, route it through a
   controlled egress proxy that blocks loopback, private, link-local, reserved,
   and metadata destinations and revalidates every redirect/connection. The
   host binds any allowed destination policy to the authenticated owner/task
   lease; the proxy enforces that per-lease policy rather than trusting guest
   input. No guest route reaches Delilah, internal service networks, databases,
   the runtime API, or host services.
5. Browser control uses a fresh per-task profile, separate from shared
   Browserless state, plus a broker and constrained action vocabulary
   (accessibility/visible state, click, type, wait, screenshot), not raw CDP,
   arbitrary page JavaScript, or a shell in the browser process. Treat page,
   image, downloaded-file, and OCR content as untrusted input. User takeover
   and secret entry pause model control. Financial authorization, grants, and
   receipts remain enforced in Delilah's host runtime.
6. Browser persistence is off by default. If continuity is requested, persist
   only task/user-scoped encrypted browser state with explicit retention,
   ownership, export/forget, and key-management behavior. The user's general
   workspace can remain persistent separately from ephemeral task compute.

## Phased implementation path

1. **Host hardening (started):** remove unused Docker socket/CLI authority from
   the bot; keep Browserless diagnostics loopback-bound. This reduces ambient
   privilege but is not an Item 14 worker implementation.
2. **Worker contract and threat tests:** define typed task/owner lease claims,
   fixed resource profiles, allowed operations, lifecycle state, and a fake
   runtime adapter. Test cross-owner denial, invalid runtime configuration,
   idempotent cleanup, timeout/cancel cleanup, and no guest secrets/network.
3. **No-network disposable worker:** launch one-shot code/artifact workers via
   a narrowly privileged manager with pinned images, resource caps, no host
   mounts, no runtime socket, and no network. Verify per-task filesystem
   separation and cleanup without running finance integrations.
4. **Browser worker:** add a browser broker and isolated profile only after
   egress/SSRF enforcement, prompt-injection handling, credential broker,
   user takeover, and action-specific approvals exist. Browser automation
   remains off by default until those gates pass.
5. **Optional stronger isolation:** make a microVM/runtime backend configurable
   for deployments whose threat model warrants a separate kernel. Do not make
   hypervisor support, hardware virtualization, or a second always-on VM a
   prerequisite for ordinary self-hosting.

The first three phases are practical for an open-source single-user install
and a friends-shared one-process deployment. Multi-process worker leases,
cross-host scheduling, and provider-capacity coordination remain out of scope;
Delilah supports one bot process per task database.
