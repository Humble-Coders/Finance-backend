# Briefs

One Markdown brief per feature / screen / milestone, written by the **Product Owner** with **`/draft-brief`**
and committed alongside the code. Each brief is also a GitHub issue (labelled **`brief`**) — that's where its
**number** and any discussion live.

- **Filename:** `B<issue-number>-<slug>.md` (e.g. `B24-login-screen.md`).
- **Purpose:** the Product Owner's intent — the **what & why** (plus any up-front technical steers) — that
  **seeds a ticket** and is the **yardstick `/review-ticket` measures against**.
- **Flow:** `/draft-brief` writes it → a cofounder runs **`/read-brief <#>`** to absorb it and agree a build
  plan → **`/draft-ticket`** drafts against it and stamps **`Brief: #<n>`** into the ticket → **`/review-ticket`**
  checks the ticket delivers the brief.

A brief is **product intent, not implementation** — the technical "how" is decided by the cofounder during
`/read-brief` + `/draft-ticket`, within the brief's non-negotiables.
