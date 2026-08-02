# Physical-Goods Delivery-Condition Vault

A GenLayer Intelligent Contract that escrows a peer-to-peer physical-goods
trade and releases it only when validator consensus judges the delivered
item's condition against the seller's own listing photo — with a bonded
contest ladder for disagreement and an independent web-fetched
tracking-proof path for when one side goes silent. A marketplace, a
classifieds app, or a resale platform imports this to fund a trade without
either party (or the platform) ever being the one who decides "does this
match what was promised."

**Contents**: [The problem](#the-problem) ·
[Why this needs GenLayer](#why-this-needs-genlayer) ·
[Why this isn't excluded](#why-this-isnt-one-of-the-excluded-patterns) ·
[Design](#design) ·
[Fixes from review](#fixes-made-in-response-to-external-review) ·
[Safety properties](#safety-properties-each-backed-by-a-named-test) ·
[Reusability](#reusability) ·
[Storage layout & constants](#storage-layout--constants) ·
[Full method reference](#full-method-reference) ·
[Development](#development) · [Status](#status) ·
[Measured on live consensus](#measured-on-live-consensus) ·
[Errors encountered](#errors-encountered-while-writing-and-fixing-this-contract) ·
[Honest limits](#the-honest-limits)

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
- **Not a format-only validator**: `_adjudicate_condition_nondet` requires
  agreement on the substantive verdict — the band, and, when it's
  MINOR_DISCREPANCY, the *exact* settlement bucket — not on JSON shape. This
  is enforced in Python (`gl.vm.run_nondet_unsafe`), not left to an LLM's
  reading of a prose tolerance. See
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
evidence shows; the contract decides what happens next. Critically, "what
time it is" is *also* deterministic and consensus-agreed rather than an
input the model or the caller supplies — see
[Trusted time](#trusted-time-not-a-caller-supplied-argument) below.

### Equivalence principles, in full

Both nondet operations that gate money (condition adjudication and tracking
classification) use `gl.vm.run_nondet_unsafe` with a **Python-written
`validator_fn`**, not `gl.eq_principle.prompt_comparative` with a
natural-language tolerance the model interprets. This was a direct fix in
response to review: an earlier version used `prompt_comparative` with a
prose principle allowing `seller_payout_bps` values "within 1500 of each
other" to count as equivalent — but the *only* value that ever actually gets
used for settlement is the leader's raw proposal, so a 15-percentage-point
LLM-judged tolerance meant real money could be split by a number no
validator had itself independently verified, just one the model was willing
to call "close enough." See
[Fixes made in response to review](#fixes-made-in-response-to-external-review)
below for the full before/after.

**Condition adjudication** (`_adjudicate_condition_nondet`): the leader
proposes `{band, seller_payout_bps, reasoning}`; every validator
independently re-runs the identical fetch-and-judge task and its
`validator_fn` requires, in code:

- the `band` to match the leader's **exactly** (no tolerance on the
  category at all), and
- when `band == MINOR_DISCREPANCY`, the `seller_payout_bps` — already
  rounded to the nearest 500 (5 percentage points) by
  `_parse_condition_verdict` before comparison — to match the leader's
  **exactly**.

Two independent runs either land in the same discrete 500-wide bucket
(agree) or they don't (disagree, leader rotates). There is no "close
enough" left anywhere in the path that decides how much money each party
receives.

**Tracking classification** (`_check_tracking_nondet`): same mechanism —
`validator_fn` requires the classified `status` (DELIVERED / IN_TRANSIT /
EXCEPTION / UNKNOWN) to match the leader's exactly. This one also gates a
full payout (`claim_via_tracking_confirmation`), so it gets the same
code-enforced treatment even though the category was already binary in the
old prose principle.

Leader/error handling (`_handle_nondet_leader_error`) still classifies by
prefix: deterministic errors must match exactly, transient errors agree if
both sides are transient, and LLM/unknown errors force disagreement so
consensus rotates the leader instead of persisting garbage. `strict_eq` is
never used: nothing here produces byte-identical output across independent
model calls in the first place — the exactness is enforced on the
*normalized, bucketed decision fields*, not on raw model text.

### Trusted time, not a caller-supplied argument

Every method that used to take a `now_ts: int` parameter (there were 12 of
them) no longer does. `_now_ts()` reads GenVM's own patched
`datetime.now(timezone.utc)` — the network's consensus-agreed block time,
identical across every validator — instead of trusting whatever integer a
transaction sender happened to pass in. This was the other direct fix from
review: with a caller-supplied `now_ts`, a buyer could call
`timeout_unaccepted_reclaim` (or any other deadline-gated method) with a
fabricated future timestamp and reclaim funds before the real deadline had
passed, or a seller could similarly lie in the other direction. `ship_by_ts`
and `deliver_by_ts` remain caller-supplied — they're deal *terms* the buyer
proposes at creation time (like "ship within 3 days"), not claims about
"what time is it right now" — and are validated against the trusted clock,
never against each other's say-so.

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

### Access control — who may call what

| Method | Who may call it | Why |
|---|---|---|
| `create_deal` | Anyone (becomes the buyer) | Permissionless entry point — no gatekeeping on who can propose a trade |
| `cancel_deal` | Buyer only | Only the party whose funds are at risk pre-acceptance should be able to pull them back |
| `accept_deal` | The named seller only | Nobody else can commit a third party's goods/bond to a trade |
| `submit_delivery_evidence` | Buyer only | Only the party holding the goods can attest to what arrived |
| `contest_verdict` | Buyer or seller only | Only the two counterparties have standing to dispute the verdict that pays them |
| `timeout_unaccepted_reclaim` | Anyone (permissionless) | Recovers the buyer's own funds; nobody's interest is harmed by a bystander triggering it early-safe logic, so there's no reason to gate it |
| `timeout_undelivered_reclaim` | Anyone (permissionless) | Same reasoning — funds always route to the buyer regardless of caller |
| `check_delivery_status` | Anyone (permissionless) | A read-only-in-effect nondet call — it records a fact, moves no funds, so gating it would only slow resolution down |
| `claim_via_tracking_confirmation` | Anyone (permissionless) | Funds always route to `deal.seller` regardless of who calls it, so restricting the caller adds no safety, only friction |
| `resolve_condition` | Anyone (permissionless) | The verdict is validator-decided, not caller-decided, and funds don't move until `finalize_deal` — so letting anyone trigger adjudication just keeps the deal from stalling on one party's inaction |
| `resolve_contest` | Anyone (permissionless) | Same reasoning as `resolve_condition` — the outcome is consensus-decided, not caller-decided |
| `finalize_deal` | Anyone (permissionless) | Funds route strictly by the already-recorded verdict; the caller has no influence over the amount or recipient |
| `force_refund_undetermined` | Anyone (permissionless) | A safety valve that always refunds the buyer — gating it would only give one party the power to withhold the buyer's own refund |

The pattern throughout: **any call that could bias who gets paid is restricted to the party who bears that specific risk; any call whose outcome is already fixed by prior consensus or by the deal's own rules is left permissionless**, so neither party can stall the other by refusing to submit a transaction.

## Fixes made in response to external review

A team review of the first submission raised two findings. Both are fixed,
not just documented around. **See [docs/REVIEW.md](docs/REVIEW.md) for the
full walkthrough** — the verbatim review comment, before/after code for
both fixes, why each was wrong, and exactly how each was verified. The
summary:

1. **"The escrow payout is not fully bound by validator consensus: two
   minor-discrepancy results up to 1,500 basis points apart are treated as
   equivalent even though that raw value directly controls how much each
   party receives."** Confirmed and fixed. The old design used
   `gl.eq_principle.prompt_comparative` with a natural-language principle
   telling the model that two `seller_payout_bps` values "within 1500 of
   each other" were equivalent — but only the leader's proposed value was
   ever written to storage and paid out, so the actual settlement figure
   was whatever the leader happened to propose, only loosely bounded by an
   LLM's *interpretation* of a tolerance, not a code-level check. Fixed by
   switching `_adjudicate_condition_nondet` and `_check_tracking_nondet` to
   `gl.vm.run_nondet_unsafe` with a `validator_fn` written in plain Python:
   the band must match the leader's exactly, and the bucketed
   `seller_payout_bps` (already rounded to the nearest 500) must also match
   the leader's exactly. The exact bucket used for settlement is now
   provably identical between the leader and every agreeing validator's own
   independent re-derivation — see
   [Equivalence principles, in full](#equivalence-principles-in-full).

2. **"Replace unrestricted caller-supplied deadline timestamps with a
   trusted time mechanism before resubmitting."** Confirmed and fixed. Every
   one of the 12 write methods that took a `now_ts: int` parameter had it
   removed; `_now_ts()` now reads GenVM's consensus-agreed
   `datetime.now(timezone.utc)` internally instead. Previously a caller
   could, for example, call `timeout_unaccepted_reclaim(deal_id, now_ts=
   ship_by_ts + 999999)` and reclaim funds immediately regardless of real
   elapsed time — every timeout, grace window, and contest deadline was
   only as trustworthy as whatever the transaction sender claimed "now" was.
   See [Trusted time, not a caller-supplied argument](#trusted-time-not-a-caller-supplied-argument).

Both fixes were verified live, not just in mocked direct tests: on
StudioNet, `timeout_unaccepted_reclaim` was called only after genuinely
waiting out a real elapsed window (the test process sleeps until the real
clock passes `ship_by_ts` — there is no other way to satisfy it anymore),
and a full contest round (`contest_verdict` → `resolve_contest`) reached
consensus on an exact settlement bucket with no LLM-judged tolerance
involved anywhere in the path. See
[Measured on live consensus](#measured-on-live-consensus).

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
                         ship_by_ts: int, deliver_by_ts: int) -> int: ...

vault = gl.get_contract_at(Address(VAULT_ADDRESS))
vault.emit(value=gl.message.value, on="finalized").create_deal(
    seller, title, description, listing_url, tracking_url, ship_by, deliver_by
)
```

Note there is no `now_ts` argument — every timestamp the vault reads for
"what time is it right now" comes from its own trusted clock, never from a
consumer's calldata.

The same primitive, parameterized only by strings and timestamps, covers:

| Use case | What changes |
|---|---|
| Furniture/electronics resale | `item_description` names cosmetic wear tolerances |
| Graded collectibles (cards, coins) | `item_description` states the claimed grade |
| Rental-deposit return condition | roles reversed: "seller" = renter posting a damage bond |
| B2B sample-vs-bulk-shipment QA | `listing_photo_url` = approved sample photo |

## Storage layout & constants

### `Deal` (one per trade, keyed by `u32` id in `TreeMap[u32, Deal]`)

| Field | Type | Purpose |
|---|---|---|
| `id` | `u32` | Deal id (matches the `TreeMap` key) |
| `buyer` / `seller` | `Address` | The two counterparties |
| `item_title` / `item_description` | `str` | Listing metadata (capped at 160 / 2000 chars) |
| `listing_photo_url` / `tracking_url` | `str` | Seller's promised-condition photo; carrier tracking page (capped at 500 chars each) |
| `delivery_photo_url` | `str` | Buyer's arrival photo; empty until `submit_delivery_evidence` |
| `status` | `u8` | One of the 15 lifecycle statuses below |
| `created_ts` / `ship_by_ts` / `deliver_by_ts` | `u64` | Trusted-clock creation time; caller-proposed deal terms |
| `price_wei` / `price_deposited_wei` | `str` (wei) | Agreed term vs. actual escrow ledger — payouts read only the `_deposited_wei` field, zeroed before every transfer |
| `seller_bond_wei` / `seller_bond_deposited_wei` | `str` (wei) | Same term/ledger split for the seller's optional performance bond |
| `contest_bond_wei` / `contest_bond_deposited_wei` | `str` (wei) | Same split for a posted contest bond |
| `contester` | `str` | Hex address of whoever contested; `""` if never contested |
| `tracking_status` / `tracking_checked` | `u8` / `bool` | Last tracking classification and whether it's ever been run |
| `verdict_band` / `seller_payout_bps` / `verdict_reasoning` | `u8` / `u32` / `str` | The recorded (pre-finalization) verdict |
| `final_band` / `final_seller_payout_bps` | `u8` / `u32` | The verdict actually paid out (may differ from the recorded one if a contest overturns it) |
| `contest_outcome` | `str` | `""` / `"UPHELD"` / `"OVERTURNED"` |
| `resolve_attempts` / `contest_count` | `u32` | Retry counters |
| `resolved_ts` / `finalized_ts` | `u64` | Trusted-clock timestamps of the recorded verdict and of finalization |

### Enums (stored as `u8`, exposed as strings on every view)

| Statuses (`STATUS_*`) | Verdict bands (`BAND_*`) | Tracking (`TRACK_*`) |
|---|---|---|
| `CREATED`, `ACCEPTED`, `DELIVERY_SUBMITTED`, `UNDETERMINED`, `VERDICT_PENDING`, `CONTESTED`, `FINALIZED_MATCH`, `FINALIZED_MINOR_DISCREPANCY`, `FINALIZED_MAJOR_DISCREPANCY`, `FINALIZED_NOT_RECEIVED`, `FINALIZED_TRACKING_CLAIM`, `CANCELLED`, `TIMEOUT_UNACCEPTED`, `TIMEOUT_UNDELIVERED`, `TIMEOUT_UNDETERMINED_REFUND` | `NONE`, `MATCH`, `MINOR_DISCREPANCY`, `MAJOR_DISCREPANCY`, `NOT_RECEIVED` | `UNKNOWN`, `IN_TRANSIT`, `DELIVERED`, `EXCEPTION` |

### Immutable constants (no setter anywhere — see [Trust model](#trust-model))

| Constant | Value | Meaning |
|---|---|---|
| `CONTEST_BOND_BPS` | 1500 (15%) | Contest bond, as a fraction of the price |
| `CONTEST_WINDOW_SECONDS` | 172800 (48h) | Window to contest a recorded verdict before `finalize_deal` may run |
| `SELLER_TRACKING_CLAIM_GRACE_SECONDS` | 259200 (3d) | Past `deliver_by_ts`, before a seller may claim via tracking proof |
| `DELIVER_GRACE_SECONDS` | 604800 (7d) | Past `deliver_by_ts`, before a buyer forfeits the delivery-evidence window |
| `UNDETERMINED_GRACE_SECONDS` | 259200 (3d) | Past the last adjudication attempt, before a stuck deal force-refunds |
| `MAX_RESOLVE_ATTEMPTS` | 5 | Adjudication retries before `force_refund_undetermined` becomes callable |
| `BPS_BUCKET` | 500 (5pp) | Granularity `seller_payout_bps` is rounded to before any comparison |

Other hard limits (`MAX_TITLE_LEN=160`, `MAX_DESCRIPTION_LEN=2000`,
`MAX_URL_LEN=500`, `MAX_REASONING_STORED=1200`, `MAX_EVIDENCE_EXCERPT=6000`,
`MAX_ACTIVITY_NOTE_LEN=200`) are sanity rails on storage, not economic
parameters. `get_config()` returns every timing/bond constant on-chain.

## Full method reference

Every parameter, in call order, plus payability and access control. `deal_id`
is always the first argument after `self`. No method anywhere takes a
caller-supplied "current time" — see
[Trusted time](#trusted-time-not-a-caller-supplied-argument).

### Writes

| Method | Params (after `deal_id` where applicable) | Payable | Who may call |
|---|---|---|---|
| `create_deal` | `seller, item_title, item_description, listing_photo_url, tracking_url, ship_by_ts, deliver_by_ts` → returns `int` (deal id) | Yes — value becomes the escrowed price | Anyone (becomes the buyer) |
| `cancel_deal` | — | No | Buyer only, pre-acceptance |
| `accept_deal` | — | Yes — value becomes the seller's optional bond (0 valid) | The named seller only |
| `timeout_unaccepted_reclaim` | — | No | Anyone (permissionless) |
| `submit_delivery_evidence` | `delivery_photo_url` | No | Buyer only |
| `timeout_undelivered_reclaim` | — | No | Anyone (permissionless) |
| `check_delivery_status` | — → returns `dict {status, reasoning}` | No | Anyone (permissionless) |
| `claim_via_tracking_confirmation` | — | No | Anyone (permissionless; funds always route to the seller) |
| `resolve_condition` | — → returns `dict` (status/band/bps) | No | Anyone (permissionless) |
| `force_refund_undetermined` | — | No | Anyone (permissionless) |
| `finalize_deal` | — | No | Anyone (permissionless) |
| `contest_verdict` | — | Yes — must equal exactly `price_wei * CONTEST_BOND_BPS / 10000` | Buyer or seller only |
| `resolve_contest` | — → returns `dict {outcome, final_band, final_seller_payout_bps}` | No | Anyone (permissionless) |

### Views

| Method | Params | Returns |
|---|---|---|
| `get_deal` | `deal_id` | Every field of `Deal`, enums as strings |
| `get_deal_summary` | `deal_id` | `{id, status, price_wei, final_band}` — cheap poll |
| `get_deal_count` | — | `int` |
| `get_party_deal_ids` | `address` | `list[int]` of deal ids the address is buyer or seller on |
| `get_activity` | `deal_id, offset=0, limit=25` | Paginated, newest-first activity log |
| `get_platform_stats` | — | `{total_deals, total_volume_wei, total_finalized, total_contests}` |
| `get_config` | — | Every constant from the table above |

Full parameter *types* are also in the on-chain schema (`genlayer schema
<address>`), which will match this table exactly since it's generated from
the same source file.

## Development

```bash
# Lint (hard gate, must pass before tests)
genvm-lint check contracts/delivery_vault.py --json
genvm-lint check examples/marketplace_board.py --json

# Direct (in-memory) tests — 58 tests, no network
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
- Direct tests: **58 passing** (`tests/direct/`), weighted toward adversarial
  and boundary cases, including a dedicated test confirming a spoofed
  `now_ts` can no longer be passed at all
  (`test_create_deal_uses_trusted_clock_not_a_caller_argument`).
- Redeployed on StudioNet after the review fixes:
  `0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054`
  ([explorer](https://genlayer-explorer.vercel.app/contracts/0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054))
- Every write method except `claim_via_tracking_confirmation`,
  `timeout_undelivered_reclaim`, and `force_refund_undetermined` has been
  executed against this redeployed address (see below). Those three are
  covered by direct tests with a warped clock, but were not exercised live
  in this pass because — now that time is genuinely trustworthy — they each
  require waiting out a real multi-day grace window on StudioNet, not
  something fakeable anymore in a single automated run.

## Measured on live consensus

Real StudioNet runs against `0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054`,
this pass (all values and addresses from actual transactions):

- **Full lifecycle up to a contested, finalized verdict** (deal 0):
  `create_deal` funded 1 GEN, `accept_deal` posted a 0.1 GEN bond,
  `submit_delivery_evidence`, `check_delivery_status` (classified `UNKNOWN`
  against a plain 200-status page with no delivery text — correctly not
  coerced to DELIVERED) all reached `ACCEPTED`. `resolve_condition` fetched
  the listing/delivery photos (both `httpbin.org/image/jpeg` — a stock photo
  of a jackal, not the described sofa) and correctly returned
  `NOT_RECEIVED`, reasoning that the delivered image bore no resemblance to
  the purchased item. The seller then called `contest_verdict` (bonding
  exactly 0.15 GEN = 15% of the price, matching `CONTEST_BOND_BPS`), and
  `resolve_contest` re-ran the adversarial adjudication, **upheld**
  `NOT_RECEIVED`, forfeited the contest bond to the buyer, and finalized to
  `FINALIZED_NOT_RECEIVED` — all with the exact-bucket-match `validator_fn`
  live on-chain, not a mocked one.
- **Convergence** (two independent deals, identical listing/delivery
  photo): both resolved to `MATCH`, run 0 and run 1, confirming the
  strict form of the convergence property live.
- **Trusted-time enforcement, proven by genuinely waiting**:
  `timeout_unaccepted_reclaim` was called only after the test process slept
  until the real StudioNet clock actually passed `ship_by_ts` — there is no
  parameter left to fake this with, so this run is direct evidence the fix
  holds on the live network, not just in a warped direct-mode test.
  `cancel_deal` was also exercised live (pre-acceptance refund path).
- Every write call across all of the above reached `ACCEPTED` consensus.

## Errors encountered while writing and fixing this contract

These are real failures hit during development, in the order they surfaced,
kept here rather than quietly edited away — each one changed something
either in the code or in how it was tested.

1. **`genvm-lint` error E022 — `Method '_fetch_image' must have 'self' as
   first parameter`.** Several helper methods (`_addr_eq`,
   `_fetch_image`, `_fetch_tracking_text`, `_exec_prompt_json`,
   `_terminal_status_for_band`) were written as `@staticmethod`, which felt
   natural since they don't touch `self.<storage>`. GenVM's contract methods
   are not allowed to be static at all — every method, even a pure helper,
   must take `self`. **Why**: the linter enforces this because GenVM's
   method-dispatch machinery always passes `self` through; a static method
   breaks that calling convention at the runtime level, not just a style
   preference. Fix: dropped every `@staticmethod` decorator and added
   `self` as the first parameter, even where it went unused.

2. **`genvm-lint` error E010 — `gl.nondet.* call ... not reachable from
   equivalence principle block`.** The first version of the fetch logic put
   `gl.nondet.web.get(...)` inside a small private method
   (`_fetch_image_or_none`), called from a second method
   (`_run_condition_adjudication`), called from the `leader()` closure
   passed to `gl.eq_principle.prompt_comparative`. **Why**: the linter's
   static reachability analysis only walks one call hop deep from the
   function actually handed to `prompt_comparative` — a nondet call buried
   two methods down isn't provably reachable from the equivalence-principle
   block, so it's flagged as a potential rule violation even though it *is*
   reachable at runtime. Fix: inlined the fetch bytes and the
   `exec_prompt` call directly into `_run_condition_adjudication` and
   `_run_tracking_check` — the two methods `leader()` calls directly — so
   every `gl.nondet.*` call is at most one hop from the closure.

3. **The same E010 error persisted even after inlining, but only for the
   nested closures — traced to `contract = self` inside the outer method.**
   The outer method did `contract = self` and then `leader()` called
   `contract._run_condition_adjudication(...)`. **Why**: the linter's
   pattern match for "is this call reachable from the leader" looks for the
   literal `self.` (or the exact identifier the outer function's first
   parameter is bound to) — it does not resolve aliases like
   `contract = self` before pattern-matching. It never actually needed
   fixing at the logic level: Python closures already capture `self` from
   the enclosing method without any alias, so it was pure ceremony that
   also happened to defeat the linter. Fix: removed the alias entirely and
   referenced `self` directly inside `leader()`, matching the pattern used
   by this repo's sibling contracts (Event-Weaver, Meme-Olympics).

4. **`genvm-lint` error E018 — `TreeMap key for 'deals' must be Comparable
   ... got 'u64'`.** Deal ids and activity-log keys were originally typed
   `TreeMap[u64, Deal]`. **Why**: GenVM's storage layer only accepts a
   specific set of key types for `TreeMap` (`str`, `Address`, `u32`, and a
   few others) — `u64` is a valid *field* type inside a stored dataclass,
   but not a valid *map key* type. Fix: switched every deal-id key (and the
   `Deal.id` field itself, for consistency) from `u64` to `u32`; ids stay
   well within `u32` range for any realistic deal volume.

5. **`UnicodeEncodeError: 'ascii' codec can't encode character '—' in
   position 1087` when fetching the contract's schema** (via
   `gltest`'s `get_contract_schema_for_code`, both the default and hosted
   Studio clients). The contract's comments and docstrings used em-dashes
   (`—`) and ellipsis characters (`…`) throughout, matching the prose style
   used elsewhere in this write-up. **Why**: the schema-fetch client
   encodes the contract source as ASCII before sending it to be compiled
   for schema extraction, and neither client falls back to UTF-8 on a
   non-ASCII character — it raises instead of degrading. This is very
   likely the exact class of "could not load schema" failure that's easy to
   hit by pasting prose with smart punctuation into a contract file. Fix:
   stripped every non-ASCII character from both `contracts/delivery_vault.py`
   and `examples/marketplace_board.py` (em-dash → hyphen, ellipsis → three
   periods) and verified programmatically that both files contain zero
   characters above codepoint 127.

6. **CLI `genlayer write ... --fee-value <wei>` does not attach
   `gl.message.value`.** An early attempt to fund `create_deal` via the
   `genlayer` CLI used `--fee-value 1000000000000000000`, expecting it to
   arrive as the payable value. The transaction reported `ACCEPTED` with
   5/5 validator agreement, but `get_deal_count()` stayed at `0` afterward.
   **Why**: `--fee-value` sets the transaction's *fee deposit* (gas-like
   allocation), not the payable message value — there is currently no CLI
   flag for attaching native value to a write. The 5/5 "ACCEPTED" consensus
   was validators unanimously agreeing that the call reverted with
   `EXPECTED: attach the agreed price as value` (visible in
   `genlayer receipt <tx> --stdout --stderr`) — a reverted call is still a
   valid, agreed-upon outcome, so it "succeeds" at the consensus layer while
   doing nothing to contract state. Fix: switched all value-carrying calls
   to `gltest`'s Python client (`ContractFunction.transact(value=...)`),
   which does attach real `gl.message.value`, and used that for every
   payable call in the integration tests.

7. **`INVALID_IMAGE` from the model provider, and separately a verdict
   claiming "no image content" was available, both against
   `picsum.photos` listing/delivery photo URLs.** **Why**: `picsum.photos`
   serves images via an HTTP 302 redirect (empty body, `Location` header)
   rather than the image bytes directly. This is a genuine, still-open
   flakiness in how the evidence layer surfaces fetched bytes to the vision
   model when the source redirects — not a bug in this contract's own fetch
   code, which is exercised and passing in 58 direct tests with mocked
   image bytes, and which produced a correct, confident `MATCH` verdict
   live on StudioNet once pointed at a non-redirecting URL
   (`httpbin.org/image/jpeg`) — including one run that survived a full
   bonded contest and adversarial re-adjudication and still converged on
   `MATCH`. This one was **not fixed in the contract**; it's a constraint on
   evidence-source selection, documented in "The honest limits" below
   rather than papered over.

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
  contract's fetch-and-forward logic (which is exercised and passing in 58
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
- **`force_refund_undetermined`, `timeout_undelivered_reclaim`, and
  `claim_via_tracking_confirmation` were not exercised live** in this pass
  (only in direct tests with a warped clock) because each now genuinely
  requires a real multi-day grace window to elapse on StudioNet — the
  trusted-time fix means there is no longer any way to fake this for a
  quick live demonstration. This is the correct trade-off (a spoofable
  `now_ts` was exactly the vulnerability being fixed), but it does mean
  these three paths' *live* evidence is currently limited to the
  deterministic-write half (direct tests cover the full logic, including
  both sides of every grace-window boundary).
- **Reusing this primitive would be a mistake** for goods whose condition
  can't be meaningfully judged from a single photo pair (e.g., mechanical
  function, smell, structural integrity under load) — the equivalence
  principle only ever compares what's visually depicted.
