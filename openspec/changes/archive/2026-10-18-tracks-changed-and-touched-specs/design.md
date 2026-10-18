## Context

A track maps every changed symbol to every Spec it reaches: head-side
annotations, the link table, base-side annotations for removed symbols. A
helper annotated or linked to many Specs makes all of them "touched", and the
Specs a person meant to change are lost among them.

## Decisions

- **The rule.** A Spec of a track is *changed* when (a) the range carries an
  OpenSpec requirement delta mapped to it, or (b) it is the primary explicit
  annotation of a symbol the range added, modified or removed. The primary
  explicit annotation is the first Spec id of the first level-1 `@spec:` (or
  its synonym `@implements:`) line in the symbol's docstring — head side for
  added and modified symbols, base side for removed ones — after `@not_spec`
  negation and alias resolution, and only when that Spec passes the same
  existed-at-the-commit filter as every other mapping. A sole annotation is
  its own primary. Verb-anchored mentions (`implements x`), `@tests:` /
  `@see:` / `@references:` and link-table rows never make a Spec changed.
  Every other Spec of the track is *touched*.
- **Format.** Touched Specs go on one line,
  `Also touched: a, b <!-- livespec:touched -->`, after `Why:` / `OpenSpec:`.
  Only a line that ends with that marker is read as touched Specs: the Why's
  continuation lines are written unindented, so a bare `Also touched:` prefix
  would truncate an older Why and turn its words into Spec ids. A touched Spec
  written as a section carries the same marker on its heading. Touched Specs
  have no section and no code of their own; the code a range changed shows up
  under the changed Specs that own it and under `(no spec)`.
- **Old entries.** An entry without markers reads exactly as before: every
  section is a changed Spec. Nothing is reinterpreted.
- **Reasons survive.** A touched Spec that has a reason — given in
  `spec_reasons`, or kept from the entry being re-recorded — is written as a
  section, so recording a range again never drops prose a person wrote.
- **Index.** `track_spec.tier` (migration 26, default `changed`, so rows from
  before read as they were written). Compact touched Specs have no per-symbol
  rows.

## Risks / Trade-offs

- A function whose docstring lists several Specs credits only the first one as
  changed. The order of ids is the author's statement of which one owns the
  code; `include_touched_sections` or a per-Spec reason restores detail.
- Compact touched Specs lose per-symbol history: code reached only by touched
  Specs is not written to the file, so `get_symbol_history` does not see it
  (`include_touched_sections` records it). The track still lists the Specs.
- `counts.specs_touched` keeps its meaning (every Spec the range reached);
  the tiers are `specs_changed` and `specs_touched_only`.
