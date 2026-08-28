---
name: Feature brief
about: The Product Owner's intent for a feature / screen / milestone — the WHAT & WHY that seeds a ticket
title: "[Brief] <feature / screen / milestone>"
labels: ["brief"]
---

<!-- Written by the Product Owner with /draft-brief. Product-first — but technical steers are welcome
     (see below); you may be an engineer with a view up front. This brief is the YARDSTICK that
     /review-ticket measures the resulting ticket against, so say what you actually mean. No secrets. -->

## 🎯 What we're building & why
<!-- The user problem and the outcome you want. Plain product language, not implementation. -->

## 👤 Who it's for
<!-- The user / role, and the context they're in when they hit this. -->

## ✅ What it must do (capabilities)
<!-- The essential things the feature must let a user do, in user terms. Each of these should show up in the
     ticket's Scope / Acceptance Criteria — that's exactly what /review-ticket checks. -->
-

## 🌟 What "good" looks like
<!-- How you'll judge success — the bar for "yes, that's the feature I asked for." -->

## 🚫 Non-negotiables
<!-- Hard lines the ticket MUST honor (product or technical). A cofounder can't silently override one —
     changing a non-negotiable comes back to you. -->
-

## 🧭 Technical steers (optional)
<!-- You may have engineering suggestions up front. Put them here and tag each:
       [hard]       — a technical constraint the ticket must follow (a non-negotiable "how").
       [preference] — a strong default; a cofounder may deviate WITH a documented reason, which
                      /review-ticket surfaces for you to bless or veto.
     Leave this empty to let your cofounders own the "how" entirely. -->
-

## 🧊 Happy to defer
<!-- What you're fine NOT doing now — stops the ticket over-scoping and tells the cofounder where the edges are. -->
-

## 📎 References
<!-- Designs (attach, or note "provided by the PO"), the legacy app, PRD sections, competitors, examples. -->
-

---
**Next:** a cofounder runs **`/read-brief <this issue number>`** to absorb this and plan the build with Claude,
then **`/draft-ticket`** (which drafts against this brief and links back to it). You close the loop with
**`/review-ticket <ticket#>`**.
