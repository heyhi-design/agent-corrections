---
name: Correction pattern proposal
about: Propose a new entry for the example registry
title: "[correction] "
labels: correction-proposal
---

**The pattern**

What's the recurring mistake or fact this correction would capture? Describe
it in your own words first; the schema fields come next.

**Proposed entry**

```yaml
- id: kebab-case-id-describing-the-pattern
  category: # voice | formatting | factual | architectural | constraint | service | research | your own label
  severity: # factual | behavioral
  trigger_context: >-
    When is this correction likely to matter? Specific enough to recognize
    the pattern, general enough to catch variants of it.
  correction: >-
    The rule, stated as a positive instruction ("do X"), not a description of
    the bug ("X was broken").
  status: active
  times_fired: 1
  last_fired: "YYYY-MM-DD"
```

**Why `factual` or `behavioral`?**

Factual corrections are fast facts (an API detail, a naming convention) that
are cheap to verify and safe to auto-write. Behavioral corrections are
judgment calls about how the agent should work, and need a human review step.
Which is this, and why?

**A concrete scenario**

Even if it doesn't make it into the entry text itself, describe one real (or
realistic) situation where this would have fired. This is what a reviewer
uses to judge whether the `trigger_context` is specific enough.

**Anything to generalize before this ships?**

If the pattern comes from a real incident, check that the entry above doesn't
name a specific product, company, or person. The example registry
demonstrates the pattern; it isn't a place to publish one team's actual
internal registry.
