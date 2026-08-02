# Design

## 1. Non-determinism budget (3 operations — no more)

1. **Condition adjudication** (`_adjudicate_condition_nondet`) — fetches the listing photo and the delivery photo (contract-side, inside the nondet block) and asks the model to compare them. No deterministic form exists: "does this look like the same item in the same condition" is a visual judgment.
2. **Delivery-confirmation fetch** (`_check_tracking_nondet`) — fetches the carrier tracking page text and asks the model to classify it into a small enum (DELIVERED / IN_TRANSIT / EXCEPTION / UNKNOWN). Tracking pages have no standard schema across carriers, so this cannot be a deterministic parser without hard-coding every carrier's HTML.
3. **Contest re-adjudication** (`_adjudicate_contest_nondet`) — a second, independently-worded judgment run when either party bonds a contest. Reuses the same evidence-fetch machinery but a differently framed prompt (adversarial-review framing, not first-pass framing), so a contest is a genuine second opinion, not a re-run of the same question.

That's it. Everything else — thresholds, payout math, state transitions, access control — is deterministic.

## 2. What stays deterministic

Access control (buyer/seller/anyone-permissionless roles per method), all escrow arithmetic and bps math, every status transition, the mapping from a verdict band to a payout split, every timeout/deadline comparison, input validation, storage writes, event/activity logging, output sanitization (clamping, key normalization). The model is only ever asked "what does the evidence show" — never "who should get paid" or "what should happen now."

## 3. Equivalence principles — revised after review (code-enforced, not LLM-judged)

**Original design (superseded):** both nondet operations used `gl.eq_principle.prompt_comparative` with a natural-language principle. A team review correctly flagged this for condition adjudication: the principle told the model that two `seller_payout_bps` values "within 1500 of each other" were equivalent, but the value actually written to storage and paid out was always just the leader's raw proposal — so the real settlement figure was only as trustworthy as an LLM's *interpretation* of a tolerance, never independently verified in code.

**Current design:** both operations use `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`, where `validator_fn` is plain Python, not a prose principle:

- **Condition adjudication** (`_adjudicate_condition_nondet`): `validator_fn` requires the `band` to match the leader's exactly, and — only when `band == MINOR_DISCREPANCY` — requires `seller_payout_bps` (already rounded to the nearest 500 by `_parse_condition_verdict` before comparison) to match the leader's exactly. Two independent runs either land in the same discrete bucket or they don't; there is no numeric "close enough" left in the path that decides how much money moves.
- **Delivery-confirmation fetch** (`_check_tracking_nondet`): `validator_fn` requires the classified `status` (DELIVERED / IN_TRANSIT / EXCEPTION / UNKNOWN) to match the leader's exactly. This also gates a full payout (`claim_via_tracking_confirmation`), so it gets the same code-enforced treatment even though the old principle was already effectively binary here.
- **Contest re-adjudication** reuses `_adjudicate_condition_nondet` verbatim (same code-enforced equivalence) with a distinct adversarial prompt that explicitly instructs the model to look for reasons the first verdict might be wrong, so a contest is a genuine second opinion, not degenerate re-confirmation.

Leader-error handling (`_handle_nondet_leader_error`) still classifies by prefix: deterministic errors must match exactly, transient errors agree if both sides are transient, and LLM/unknown errors force disagreement so consensus rotates the leader rather than persisting garbage.

Numeric output is still banded (a coarse enum plus a bps split rounded to the nearest 500) rather than a raw float — this is the "confidence HIGH not 0.83" pattern — but the comparison against that bucket is now a Python `==`, not an LLM's judgment call. `strict_eq` is still not used anywhere: nothing here produces byte-identical raw model text across independent calls; the exactness is enforced on the normalized, bucketed decision fields instead.

## 4. Failure and abstention semantics

- **A failed image fetch is never "the item doesn't exist" or "condition is bad."** It is recorded as `NOT_RECEIVED`-adjacent evidence only when *both* required photos are unreachable; if only the tracking fetch fails, the condition judgment still proceeds on photos alone and the tracking status is recorded as `UNKNOWN`, never coerced to DELIVERED or EXCEPTION.
- **Unparseable model output** raises `LLM_ERROR:` inside the leader closure, which the validator's independent re-derivation naturally disagrees with unless it *also* fails identically — forcing leader rotation rather than persisting a guess (mirrors the `_handle_leader_error` pattern from Meme-Olympics).
- **Explicit "we don't know" state:** `UNDETERMINED` is a first-class deal status, distinct from any resolved band. A deal can sit in `UNDETERMINED` — money is not stranded there; see §8.
- **Safe failure direction:** ambiguous evidence never defaults to releasing the seller's funds. `MINOR_DISCREPANCY` at the lowest confidence still nets the seller *something* (goods did arrive), but a genuinely unreadable/contradictory verdict lands in `UNDETERMINED`, which resolves toward the buyer's favor after a retry window (see timeout ladder) rather than toward whichever party called the resolving transaction.

## 5. Storage layout

`TreeMap[u64, Deal]` keyed by sequential deal id, `@allow_storage` dataclass with only `str` / `u8` / `u32` / `u64` / `u256` / `bool` fields (photo URLs and reasoning are length-capped strings; no nested lists inside the dataclass — the two photo URLs are two separate fixed fields, not a `DynArray`, since the count is permanently capped at exactly one listing photo and one delivery photo). An append-only `DynArray[ActivityEvent]` per deal, capped read-side by pagination. No unbounded structure is ever iterated in full; `TreeMap` lookups are O(1) by id.

## 6. Consumer interface

Push vs. pull: **pull**. A consumer reads `get_deal(id)` for the current status/verdict rather than registering a callback, because (a) resolution can retry (`UNDETERMINED` → re-resolve) so a single push could fire the wrong final state, and (b) GenVM's documented `__receive__`/`__handle_undefined_method__` hooks are rejected by `genvm-lint`, so there is no clean push primitive available anyway (see gotcha #14 in the build brief). A marketplace UI or another contract polls `get_deal` / `get_deal_summary`.

```python
@gl.contract_interface
class DeliveryVault:
    class View:
        def get_deal(self, deal_id: int) -> dict: ...
        def get_deal_summary(self, deal_id: int) -> dict: ...
    class Write:
        def create_deal(self, seller: str, item_title: str, item_description: str,
                         listing_photo_url: str, tracking_url: str,
                         ship_by_ts: int, deliver_by_ts: int) -> int: ...
        def accept_deal(self, deal_id: int) -> None: ...
```

## 7. Trust model

Privileged roles are deliberately thin: **buyer** (funds the deal, submits delivery evidence, can cancel pre-acceptance), **seller** (accepts the deal, can post an optional performance bond), and **anyone** (resolution and timeout-recovery calls are permissionless once their preconditions hold — neither party can block or delay the other's payout by staying silent). There is no contract owner, no admin pause, no fee sweep, no upgrade path with a privileged upgrader — the primitive holds no configuration a party could bias in their own favor. The only asymmetric power either party has is *initiating a contest*, and that costs a bond that is forfeited if the contest fails, which bounds frivolous use without banning honest disputes (monotonic cost, not a permission gate).

## 8. Funds in every terminal state

| Terminal state | Where the money rests |
|---|---|
| `RESOLVED_MATCH` | Full price → seller; seller's bond (if any) returned to seller |
| `RESOLVED_MAJOR_DISCREPANCY` | Full price refunded → buyer; seller's bond → buyer (compensates the wasted trip) |
| `RESOLVED_MINOR_DISCREPANCY` | Price split buyer/seller per `seller_payout_bps`; seller's bond returned to seller |
| `RESOLVED_NOT_RECEIVED` | Full price + seller's bond → buyer |
| `CANCELLED` (pre-acceptance) | Full price refunded → buyer |
| `TIMEOUT_UNACCEPTED` (seller never accepted by a grace deadline) | Full price refunded → buyer, permissionless |
| `TIMEOUT_UNDELIVERED` (buyer never submits delivery evidence, tracking never shows DELIVERED, past `deliver_by_ts` + grace) | Full price refunded → buyer, permissionless — protects the buyer, who is the party without the goods |
| `TIMEOUT_UNRESOLVED` (delivery evidence submitted but never resolved past a grace window) | Anyone may call `resolve_condition` permissionlessly — this state is transient, not terminal |
| Contest fails (verdict upheld) | Contester's bond → the non-contesting counterparty |
| Contest succeeds (verdict overturned) | Contester's bond returned; funds re-routed per the new verdict |

No state leaves value with no defined recipient.

## 9. Latency budget

`resolve_condition` is one nondet round with two fetches (listing + delivery photo) plus one exec_prompt call ≈ 2–4 minutes. `check_delivery_status` (tracking fetch) is a separate, cheaper single-fetch round a buyer or seller can call independently before condition resolution, so the expensive image round is never blocked behind a slow carrier page. A contest adds one more full round. Registration (`create_deal`, `accept_deal`, evidence submission) is pure deterministic writes, ~20–40s each, deliberately separated from the slow resolution step so nobody is blocked waiting on consensus just to register a deal.
