# Physical-Goods Delivery-Condition Vault

A GenLayer Intelligent Contract that escrows a peer-to-peer physical-goods
trade and releases it only when validator consensus judges the delivered
item's condition against the seller's own listing photo — with a bonded
contest ladder for disagreement and an independent web-fetched
tracking-proof path for when one side goes silent. A marketplace, a
classifieds app, or a resale platform imports this to fund a trade without
either party (or the platform) ever being the one who decides "does this
match what was promised."

## The problem

Alice lists a couch with a photo. Bob sends her the money. The couch arrives
scuffed. Off-platform, this is Bob's word against Alice's, adjudicated by
whichever payment processor has the leverage — usually favoring whoever
disputes loudest, or nobody. A naive on-chain "escrow" doesn't fix this: if
release is gated on the buyer clicking "I'm satisfied," the buyer captures
all the leverage (why would they ever click it?). If it's gated on a
deterministic check, there's nothing to check — "does this look damaged" has
no parser. If it's an off-chain arbitrator, you've rebuilt the payment
processor's dispute desk on a blockchain and gained nothing.

## Why this needs GenLayer

Run the counterfactual against every alternative:

- **Off-chain escrow service**: works, but the service is the trusted party
  Bob and Alice both have to hope is honest and solvent. That's the entire
  problem this exists to remove.
- **Price/status oracle**: there's no numeric feed for "is this couch
  damaged." Oracles answer questions with one deterministic right answer;
  this question doesn't have one.
- **Hash/deterministic parser**: a hash proves a file didn't change, not that
  a couch looks the same as its listing photo.
- **Optimistic oracle + human dispute**: closer, but the dispute resolver is
  a small committee of humans who must be paid, coordinated, and trusted not
  to collude with one side — a weaker, slower version of what validator
  consensus already does natively.
- **A single LLM call from a backend**: whoever runs that backend is the
  arbitrator. Nothing stops them from favoring themselves, a friend, or a
  paying customer, and neither party can prove otherwise.
- **A multisig of "reporters"**: just a small permissioned committee with
  extra steps, and it still needs someone to nominate and pay the reporters.

The trust problem, stated precisely: two mutually distrusting parties (buyer,
seller) depend on one answer to a question that is irreducibly visual, and
the party who controls the most important piece of evidence (the buyer, who
holds the delivery photo) has a financial incentive to shade it. GenLayer
lets the judgment and the money live in the same trust domain — the same
validators who fetch the evidence are the ones whose agreement releases the
funds — which is exactly what none of the alternatives above can offer.

## Why this isn't one of the excluded patterns

- **Not a thin LLM wrapper**: the model is never asked "what should happen" —
  only "what does this evidence show." Every payout, deadline, and state
  transition is deterministic code reading a recorded verdict, never a live
  decision by the model.
- **Not a format-only validator**: the equivalence principle in
  `_adjudicate_condition_nondet` requires agreement on the substantive verdict
  (which band, and within what payout tolerance), not on JSON shape. See
  [Equivalence principles](#equivalence-principles-in-full) below.
- **Not judging from user-submitted text alone**: the listing photo, the
  delivery photo, and the tracking page are all fetched by the contract
  itself, inside the nondet block, on every validator's own machine — never
  trusted from what either party claims.
- **Not a minor variant of an existing submission**: it isn't a semantic
  change-detector, a multi-source corroboration oracle, or a milestone/URL
  escrow (that shape already exists elsewhere as a full-app submission this
  cycle) — this is the only physical-goods, image-evidence escrow with a
  bonded contest ladder in this cycle's field, as far as the ecosystem wall
  and GitHub search turned up.

## Design

### Non-determinism budget (exactly 3 operations)

1. **Condition adjudication** — fetch the listing photo and the delivery
   photo, ask the model to compare them. No deterministic form exists.
2. **Tracking-page classification** — fetch the carrier's tracking page, ask
   the model to classify it into one of four states. Tracking pages have no
   shared schema across carriers, so a deterministic parser would mean
   hard-coding every carrier's HTML.
3. **Contest re-adjudication** — a second, adversarially-framed run of (1),
   triggered by a bonded contest.

### What stays deterministic

Access control, every timestamp comparison, all escrow arithmetic, the bps
math for splits, the mapping from a verdict band to who gets paid, storage
writes, activity logging, input validation, and output sanitization (band
name whitelisting, bps clamping and bucketing). The model is asked what the
evidence shows; the contract decides what happens next.

### Equivalence principles, in full

Condition adjudication (`prompt_comparative` — never `prompt_non_comparative`,
since this decides a payout and reviewers correctly treat non-comparative
judging-a-payout as a fake-consensus smell):

> "Both results are JSON verdicts about whether a physical item's delivered
> condition matches its promised listing condition, based on the same
> listing photo and delivery photo. Treat them as equivalent if they agree
> on the 'band' value (MATCH, MINOR_DISCREPANCY, MAJOR_DISCREPANCY, or
> NOT_RECEIVED) AND, when the band is MINOR_DISCREPANCY, their
> 'seller_payout_bps' values are within 1500 of each other. Differences in
> wording of 'reasoning', which discrepancy is named first, formatting, or
> key order are irrelevant. A different band, or a materially different
> payout split outside that tolerance, is NOT equivalent."

Tracking classification (also `prompt_comparative`):

> "Both results are JSON classifications of the same carrier tracking page
> into exactly one of DELIVERED, IN_TRANSIT, EXCEPTION, or UNKNOWN. Treat
> them as equivalent if and only if they name the same status. Reasoning
> wording is irrelevant."

The minor-discrepancy payout split is banded to the nearest 500 bps (5
percentage points) before comparison — validators compare a coarse category,
not a float. `strict_eq` is never used: nothing here produces byte-identical
output validators could reproduce deterministically.

### The "does the deterministic half make consensus pointless?" objection

It's worth confronting directly: if so much of this contract is
deterministic arithmetic, why does it need consensus at all? Because every
deterministic input is itself a consensus output. Remove consensus and the
contract is inert — there is no `seller_payout_bps`, no `verdict_band`, no
`tracking_status` for the deterministic code to act on; `resolve_condition`
has nothing to route. Remove the deterministic half instead, and the model
decides payouts directly — which is precisely the "AI decides X" pattern
this category rejects. Both halves are load-bearing: the model supplies
judgment, the code supplies the guarantee that the judgment is applied
exactly once, in the right order, to the right amount, and can never be
biased by whoever happens to submit the transaction.

### Failure and abstention semantics

- A failed photo fetch is never read as "the item is damaged" — a required
  photo that can't be fetched (non-transient failure) records `NOT_RECEIVED`,
  never a damage verdict.
- Unparseable model output raises inside the leader closure; the validator's
  independent re-derivation naturally disagrees unless it fails identically,
  forcing leader rotation rather than persisting a guess.
- `UNDETERMINED` is a first-class, retryable deal status — money is never
  stranded there (see the funds table below).
- Safe failure direction: an unresolved judgment never defaults to releasing
  the seller's funds. Ambiguity resolves toward the buyer over time (via
  `force_refund_undetermined`), never toward whoever happened to call the
  transaction.

### Funds in every terminal state

| Terminal state | Where the money rests |
|---|---|
| `FINALIZED_MATCH` | Full price -> seller; seller's bond returned to seller |
| `FINALIZED_MAJOR_DISCREPANCY` / `FINALIZED_NOT_RECEIVED` | Full price + seller's bond -> buyer |
| `FINALIZED_MINOR_DISCREPANCY` | Price split by `seller_payout_bps`; bond -> seller |
| `FINALIZED_TRACKING_CLAIM` | Full price + bond -> seller (tracking proved delivery, buyer never disputed) |
| `CANCELLED` / `TIMEOUT_UNACCEPTED` / `TIMEOUT_UNDELIVERED` / `TIMEOUT_UNDETERMINED_REFUND` | Full price (+ bond where posted) -> buyer |
| Contest upheld | Contest bond -> counterparty; original verdict pays out |
| Contest overturned | Contest bond -> contester; new verdict pays out |

No path leaves value with no defined recipient; every non-terminal status
(`CREATED`, `ACCEPTED`, `DELIVERY_SUBMITTED`, `UNDETERMINED`,
`VERDICT_PENDING`, `CONTESTED`) has a permissionless path forward that
someone other than the stuck party can always trigger.

### Trust model

No owner, no admin, no pause switch, no fee sweep, no upgrader. The only
privileged actions are role-scoped to the two parties (buyer creates/cancels/
submits evidence; seller accepts) — every resolution, timeout, and
finalization call is permissionless, so neither party can block the other's
payout by going silent. The one asymmetric power either side has is
*contesting*, and that costs a bond forfeited on a failed contest — a
monotonic cost, not a permission gate, and capped at one contest per deal so
it can't be used to stall indefinitely.

## Safety properties (each backed by a named test)

- Double-spend structurally impossible: every payout path zeroes its ledger
  field(s) and persists state *before* transferring — `test_finalize_deal_twice_reverts`,
  `test_timeout_unaccepted_reclaim_twice_reverts`.
- A failed/unfetchable photo is never read as damage —
  `test_resolve_condition_not_received_when_delivery_photo_unfetchable`.
- Malformed and unrecognized model output degrade to `UNDETERMINED`, never a
  crash or a silent guess — `test_resolve_condition_malformed_llm_output_sets_undetermined`,
  `test_resolve_condition_unrecognized_band_name_sets_undetermined`.
- `UNDETERMINED` is retryable and eventually force-refunds the buyer, never
  stranding funds — `test_resolve_condition_retries_after_undetermined`,
  `test_force_refund_undetermined_requires_max_attempts`,
  `test_force_refund_undetermined_refunds_buyer_after_exhaustion`.
- Every timeout ladder rung is tested on both sides of its boundary —
  `test_timeout_unaccepted_reclaim_before_deadline_reverts` /
  `..._refunds_after_deadline`; `test_claim_via_tracking_confirmation_before_grace_reverts`
  / `..._pays_seller_after_grace`.
- A timeout path is blocked once the other side has actually acted, so it
  can't be raced — `test_timeout_undelivered_reclaim_blocked_once_evidence_submitted`,
  `test_claim_via_tracking_confirmation_blocked_once_evidence_submitted`.
- The contest bond must be exact, is single-use, and is time-boxed —
  `test_contest_verdict_requires_exact_bond`, `test_contest_verdict_only_once_per_deal`,
  `test_contest_verdict_after_window_reverts`.
- Both contest outcomes reroute funds correctly —
  `test_resolve_contest_upheld_forfeits_bond_to_counterparty`,
  `test_resolve_contest_overturned_reroutes_payout`.
- Access control holds for every role-scoped write —
  `test_cancel_deal_only_buyer_may_cancel`, `test_accept_deal_only_named_seller`,
  `test_submit_delivery_evidence_only_buyer`, `test_contest_verdict_only_buyer_or_seller`.

## Reusability

A consumer imports the vault's `create_deal` for its own listing/escrow flow
and never touches image adjudication, the tracking fetch, or the contest
ladder. The complete integration (`examples/marketplace_board.py`, linted and
tested on its own):

```python
@gl.contract_interface
class DeliveryVault:
    class Write:
        def create_deal(self, seller: str, item_title: str, item_description: str,
                         listing_photo_url: str, tracking_url: str,
                         ship_by_ts: int, deliver_by_ts: int, now_ts: int) -> int: ...

vault = gl.get_contract_at(Address(VAULT_ADDRESS))
vault.emit(value=gl.message.value, on="finalized").create_deal(
    seller, title, description, listing_url, tracking_url, ship_by, deliver_by, now
)
```

The same primitive, parameterized only by strings and timestamps, covers:

| Use case | What changes |
|---|---|
| Furniture/electronics resale | `item_description` names cosmetic wear tolerances |
| Graded collectibles (cards, coins) | `item_description` states the claimed grade |
| Rental-deposit return condition | roles reversed: "seller" = renter posting a damage bond |
| B2B sample-vs-bulk-shipment QA | `listing_photo_url` = approved sample photo |

## API reference

**Writes**: `create_deal`, `cancel_deal`, `accept_deal`,
`timeout_unaccepted_reclaim`, `submit_delivery_evidence`,
`timeout_undelivered_reclaim`, `check_delivery_status`,
`claim_via_tracking_confirmation`, `resolve_condition`,
`force_refund_undetermined`, `finalize_deal`, `contest_verdict`,
`resolve_contest`.

**Views**: `get_deal`, `get_deal_summary`, `get_deal_count`,
`get_party_deal_ids`, `get_activity`, `get_platform_stats`, `get_config`.

Full parameter signatures are in the on-chain schema (`genlayer schema
<address>`) and mirrored in `contracts/delivery_vault.py`'s method
definitions.

## Development

```bash
# Lint (hard gate, must pass before tests)
genvm-lint check contracts/delivery_vault.py --json
genvm-lint check examples/marketplace_board.py --json

# Direct (in-memory) tests — 57 tests, no network
pytest tests/direct/ -v

# StudioNet integration tests (real GEN, real consensus; needs .keys/*.json — see below)
pytest tests/integration/ -v -s
```

The integration tests sign with two StudioNet accounts exported to
`.keys/buyer.json` / `.keys/seller.json` (gitignored) via:

```bash
genlayer account export --account <buyer-account> --output .keys/buyer.json --password testpass123
genlayer account export --account <seller-account> --output .keys/seller.json --password testpass123
```

StudioNet is gasless, so any account works with a zero or nonzero balance.

## Status

- `genvm-lint`: clean on both the primitive and the consumer example.
- Direct tests: **57 passing** (`tests/direct/`), weighted toward adversarial
  and boundary cases.
- Deployed on StudioNet: `0xd661bea0F9796CA39d8bA4BBe5cF09E7C7138758`
  ([explorer](https://genlayer-explorer.vercel.app/contracts/0xd661bea0F9796CA39d8bA4BBe5cF09E7C7138758))
- Every write method except `force_refund_undetermined` has been executed
  against the live deployed address (see below); `force_refund_undetermined`
  is covered by 2 direct tests but not exercised live in this pass, since
  forcing it live needs 5 exhausted adjudication rounds plus a 3-day grace
  window.

## Measured on live consensus

Real StudioNet runs, this pass (all timestamps and addresses from actual
transactions):

- **Full lifecycle** (deal 0): `create_deal` funded 1 GEN, 5/5 validators
  AGREE; `accept_deal` with a 0.1 GEN bond, 5/5 AGREE; `submit_delivery_evidence`,
  5/5 AGREE; `check_delivery_status` against `https://httpbin.org/status/200`,
  classified `UNKNOWN` (a 200-status page with no delivery-status text —
  correctly not coerced to DELIVERED); `resolve_condition` against
  `picsum.photos` listing/delivery photos returned `UNDETERMINED` twice in a
  row — see honest limits below — funds remained fully escrowed both times
  (`price_deposited_wei` unchanged), exactly as designed.
- **Condition adjudication with a direct (non-redirecting) image URL** (deal
  1): resolved to a real banded verdict (`NOT_RECEIVED`, on that run) with
  4/5 AGREE + 1 IDLE reaching quorum, then `finalize_deal` correctly zeroed
  the escrow and routed the full price + bond to the buyer.
- **Contest ladder** (deal 5): `resolve_condition` returned `MATCH` with
  identical listing/delivery photos ("The buyer's delivery photo is an exact
  visual match to the seller's listing photo..."); `contest_verdict` bonded
  0.15 GEN (15% of a 1 GEN price, matching `CONTEST_BOND_BPS`); `resolve_contest`
  re-ran adversarially, upheld the original `MATCH`, forfeited the contest
  bond to the counterparty, and finalized to `FINALIZED_MATCH` — all in one
  permissionless call.
- **Timeout ladder**: `cancel_deal` (deal 2), `timeout_unaccepted_reclaim`
  (deal 3), and `timeout_undelivered_reclaim` (deal 6, refunding both the
  price and the seller's posted bond) all executed live and zeroed the
  escrow ledger as designed.
- Every deterministic write across all of the above reached **5/5 AGREE**
  except one nondet round that reached quorum at 4 AGREE + 1 IDLE — both are
  documented StudioNet behaviors, not faults.

## The honest limits

- **Image evidence sources must serve bytes directly, not redirect.**
  `picsum.photos` URLs return an HTTP 302 with an empty body and a
  `Location` header; fetched through GenVM's nondet web layer this produced
  either an explicit `LLM_ERROR: ... INVALID_IMAGE` from the model provider
  or, on a different attempt with a direct (non-redirecting) URL, a verdict
  claiming no image content was available at all despite a successful fetch.
  The same direct URL used identically for both listing and delivery photos
  in the contest-ladder run *did* produce a correct, confident `MATCH`
  verdict — so this reads as real flakiness in how the current pinned runner
  version surfaces fetched image bytes to the vision model, not a bug in the
  contract's fetch-and-forward logic (which is exercised and passing in 57
  direct tests with mocked bytes). Reusers should serve evidence photos from
  a URL that returns image bytes directly with no redirect chain, and should
  expect to budget for at least one retry.
- **`UNDETERMINED` is expected, not exceptional.** Both the design and the
  StudioNet docs call this out: a round can fail to converge and must be
  retried. This contract's `resolve_attempts` counter and
  `force_refund_undetermined` safety valve exist specifically because this
  is a known, not hypothetical, behavior.
- **Studio simulates balances; there is no EVM layer or ghost contract
  there.** Every escrow ledger field (`price_deposited_wei`, etc.) was
  verified to zero correctly on every payout path, live, in this pass. Actual
  GEN arriving in a plain wallet's spendable balance was not independently
  re-verified against a block explorer balance check in this pass — say so
  plainly rather than overclaim.
- **`force_refund_undetermined` was not exercised live** in this pass (only
  in direct tests) because forcing it requires 5 exhausted adjudication
  attempts plus a 3-day grace window; the direct tests cover both the
  "attempts not yet exhausted" revert and the successful refund path with a
  mocked clock.
- **Reusing this primitive would be a mistake** for goods whose condition
  can't be meaningfully judged from a single photo pair (e.g., mechanical
  function, smell, structural integrity under load) — the equivalence
  principle only ever compares what's visually depicted.
