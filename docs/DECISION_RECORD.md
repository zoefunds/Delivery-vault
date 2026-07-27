# Decision record

## Candidates generated (12, spanning 5 capabilities)

1. Freelance milestone escrow judged against a live deployed URL — *web + escrow*
2. Bug-bounty escrow judged against a live repo diff/commit — *web + escrow*
3. Cross-border benefit/visa-status conditional remittance — *web + escrow*
4. API SLA bond, slashed against the vendor's own status page — *web + value/slashing*
5. **Physical-goods delivery-condition vault, judged against listing vs. arrival photos** — *image + escrow*
6. Screenshot-verified sports/scoreboard bet settlement — *image + value*
7. EVM-gated insurance claim unlocking an Ethereum-side vault — *EVM interop + escrow*
8. Factory for parametric flight-delay/weather insurance, one child IC per route — *factories + web + value*
9. Cross-contract escrow arbitration primitive, any IC can request a judged split — *composition + escrow*
10. Semantic grant-to-bounty matching via embeddings — *embeddings* (weak Gate B — no adversarial counterparty)
11. AI sentiment score for DAO proposals — *rejected outright, Gate E fails (advice, not a consequence)*
12. Reputation-bonded content-moderation stake, slashed on judged rule violation — *value*, Gate C weak (too subjective/format-checkable)

Capabilities represented: web+escrow (1,2,3,4), image+value (5,6), EVM interop (7), factories (8), composition (9), embeddings (10). Five distinct capabilities, two candidates involving native value beyond simple escrow (4 slashing, 8 factories-of-value).

## Self-audit

- **Most similar pair:** #1 and #2 — both are "judge a live artifact against a spec," differing only in domain (freelance vs. bounty). Kept as one underlying primitive with two use cases, not two primitives — this is exactly the shape Delivera (a full-app submission already built and submitted separately) occupies, which is why neither was chosen here.
- **Pick without web access:** #9 (cross-contract arbitration primitive) — most general, but too abstract to stand alone without a canonical evidence source; the ask specifically wants both a nondet judgment and web fetch as first-class features, not an evidence-source-agnostic shell.
- **Strongest discard:** #7 (EVM-gated insurance) — genuinely interesting, but the funds-holding half would live on the EVM side, leaving the IC as a thin trigger rather than the primitive that actually holds the money.

## Chosen: #5 — Physical-Goods Delivery-Condition Vault

### Gate screening

- **Gate A (counterfactual):** Delete GenLayer and this becomes "the buyer decides if they're happy" (buyer captures all the leverage — they hold the goods and the money's fate) or "a marketplace arbitrator eyeballs photos" (single trusted party, exactly what escrow exists to avoid). Physical condition disputes are the canonical case a backend script cannot resolve: there is no API for "does this look like the same couch, undamaged."
- **Gate B (trust):** Buyer and seller are mutually distrusting counterparties in a P2P trade (no marketplace platform mediating). The buyer controls the input that matters most (the delivery-condition photo) and has a financial incentive to claim damage that isn't there; the seller controls the listing photo and has an incentive to have posed it favorably. Neither should be trusted alone.
- **Gate C (judgment):** "Does the item that arrived match the promised condition" is irreducibly visual/semantic — lighting, angle, wear, and packaging all matter, and no deterministic parser or hash comparison can answer it.
- **Gate D (importable):** A consumer contract funds a vault with `create_deal(seller, listing_photo_url, tracking_url, ship_by_ts)` + value, and later reads `get_deal(id)` for the verdict — under 10 lines, and works whether the goods are a couch, a graded card, or a phone.
- **Gate E (consequential):** The verdict directly gates a GEN payout split between buyer and seller.
- **Gate F (originality):** Distinct from this cycle's collision list (not semantic change-detection over time, not multi-source corroboration/reputation). Distinct from Delivera (freelance/digital-deliverable milestones judged against a URL) and from Event-Weaver/Meme-Olympics (prediction markets / judged competitions) — this is the only physical-goods, image-evidence escrow among them.

### Why this is not a rejected pattern

- Not a hello-world/thin wrapper: full escrow ledger discipline (deposited vs. term fields), a bonded contest ladder, deterministic timeout/recovery paths, and two independent nondet surfaces (image judgment + web-fetched tracking confirmation).
- Not "AI decides X" as an app: the model's output is *never* read by a human and acted on manually — it directly routes GEN inside the same transaction, deterministically, per Gate E.
- Not a format-only validator: the equivalence principle in `_adjudicate_condition_nondet` compares the *substance* of the visual judgment (band + which discrepancies were named), not JSON shape.
- Not resolving from user-submitted text alone: both the listing and delivery photos, and the tracking page, are fetched *by the contract* inside the nondet block — never trusted from calldata.
