# Build gates

Per-ticket definition-of-done checks written by the critic session during the build: signature
diffs against `TAD.md` §3, schema strictness, router status codes, app-factory singletons, and
per-module smoke checks (`smoke/`). They are evidence that every ticket was gated — not the
business-rule tests, which live one level up in `tests/`. To run only the meaningful suite:
`pytest tests --ignore=tests/gates`.
