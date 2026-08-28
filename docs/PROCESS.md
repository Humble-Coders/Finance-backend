# Team Process — How We Build with Claude Code

This is the playbook for running this project. It's written so a **new teammate can read it once and run the whole pipeline.** The process lives **here and in the Humble Task Force Claude Code plugin — not in any one person's head.** Anyone who installs the plugin and clones the repo inherits it.

> **New here? Read this, then `docs/PRD.md` (the product) and `CLAUDE.md` (architecture).** That's enough to start.

## Roles
Three levels — each owns the layer below and answers to the one above:
- **Product Owner** — owns the **what & why**. Sets the foundation (PRD + `CLAUDE.md`), captures each feature's intent as a **brief**, and reviews drafted tickets against that intent. Has the final call; doesn't do the technical reading.
- **Manager** (delivery lead / cofounder) — owns the **how**. Absorbs a brief, decides the technical approach, drafts the ticket, and reviews the finished PR.
- **Developer** — executes a ticket with Claude Code, hands off a report, opens a PR.

*(A solo founder can wear all three hats — the roles are about which hat you're wearing, not headcount.)*

## The pipeline — two gates
```
Product Owner:  /draft-prd → /draft-architecture → /draft-brief ─┐
                                                                 │  BRIEF #n
Manager:                        /read-brief #n → build plan → /draft-ticket ─┐
                                                                             │  TICKET (Brief: #n)
Product Owner:  ┌──────────────── /review-ticket ◀────────────────────────────┘   ← GATE 1: ticket vs brief
                │ sign off
Developer:      └─▶ /start-ticket → build → /handoff → PR ─┐
                                                           │
Manager:                       /manager-review ◀───────────┘   ← GATE 2: PR vs ticket  →  merge
```
Each gate compares an artifact to the intent above it: **Gate 1** — does the *ticket* deliver the *brief*? **Gate 2** — does the *PR* deliver the *ticket*?

## 0. Foundation — once per project
- **`/setup-tickets`** — scaffolds the templates, folders, the `brief` label, and this doc.
- **`/draft-prd <idea>`** — interviews the Product Owner to write `docs/PRD.md` (the product spec).
- **`/draft-architecture`** — turns the PRD + codebase into `CLAUDE.md` (architecture + rules every dev's Claude reads automatically).

These ground everything — briefs, tickets, and every `/start-ticket` read them. **Do this first.**

## 1. Framing a feature — Product Owner
Run **`/draft-brief <feature>`**. Claude interviews you for the **what & why** — the user problem, the capabilities it must have, what "good" looks like, your **non-negotiables**, and any **technical steers** you have up front (tagged `[hard]` or `[preference]`). It writes the brief as a GitHub issue (labelled `brief`) + a committed `docs/briefs/B<#>-*.md`. That brief seeds the ticket and is the yardstick you'll review against.

## 2. Absorbing the brief + planning — Manager
Run **`/read-brief <brief#>`**. Claude gives a deep product walkthrough so you *feel* what the Product Owner wants, answers your questions (training mode), then proposes a **build plan** — the technical approach and how to split it into ticket(s). You make the "how" decisions here, within the brief's non-negotiables.

## 3. Drafting the ticket — Manager
Run **`/draft-ticket`**. Claude drafts the technical ticket **against the brief**, interviews you for the developer-facing decisions (access, hosting, scope), stamps `Brief: #<n>`, and creates the issue.
- **Definition of Ready:** runnable by a junior dev cold.
- **Honor the brief's non-negotiables** — don't silently override one; flag it back to the Product Owner.
- **Secrets never go in tickets** — say *what* and *where*, never the value.

## 4. GATE 1 — Product Owner reviews the plan
Run **`/review-ticket <ticket#>`**. Claude checks the ticket against the brief — intent coverage, non-negotiables honored, silent scope changes, and the decisions that carry product risk — **in product language**. Sign off, or send back with product-level asks. It does *not* judge code quality (that's Gate 2).

## 5. Executing — Developer
Run **`/start-ticket <issue#>`**. Claude reads the ticket (+ the linked brief for the *why*) + `CLAUDE.md` + PRD, gives a plain-language walkthrough, offers **training-mode Q&A**, proposes a plan you confirm, then builds on a `ticket-<#>-<slug>` branch.

## 6. Handing off — Developer
Run **`/handoff <issue#>`** — writes `handoffs/ticket-<#>.md` from the **real git diff**. Open a PR; the template links the handoff.

## 7. GATE 2 — Manager reviews the code
Run **`/manager-review <PR#>`** — Claude checks the diff + handoff against the ticket's acceptance criteria and flags risks at `file:line`. Run the app yourself. Approve or request changes, then merge.

## Onboarding a new teammate (the whole point)
1. Install the plugin: `/plugin marketplace add Humble-Coders/humble-task-force` then `/plugin install humble-task-force@humble-coders`.
2. Get repo access; `gh auth login`.
3. Read this file, `docs/PRD.md`, and `CLAUDE.md`.
4. You now have the full pipeline — foundation to review. **Nothing is stored in a person; it's all in the plugin + the repo.**
