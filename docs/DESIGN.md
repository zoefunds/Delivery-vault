# Design

## 1. Non-determinism budget (3 operations — no more)

1. **Condition adjudication** (`_adjudicate_condition_nondet`) — fetches the listing photo and the delivery photo (contract-side, inside the nondet block) and asks the model to compare them. No deterministic form exists: "does this look like the same item in the same condition" is a visual judgment.
2. **Delivery-confirmation fetch** (`_check_tracking_nondet`) — fetches the carrier tracking page text and asks the model to classify it into a small enum (DELIVERED / IN_TRANSIT / EXCEPTION / UNKNOWN). Tracking pages have no standard schema across carriers, so this cannot be a deterministic parser without hard-coding every carrier's HTML.
3. **Contest re-adjudication** (`_adjudicate_contest_nondet`) — a second, independently-worded judgment run when either party bonds a contest. Reuses the same evidence-fetch machinery but a differently framed prompt (adversarial-review framing, not first-pass framing), so a contest is a genuine second opinion, not a re-run of the same question.

That's it. Everything else — thresholds, payout math, state transitions, access control — is deterministic.

## 2. What stays deterministic

Access control (buyer/seller/anyone-permissionless roles per method), all escrow arithmetic and bps math, every status transition, the mapping from a verdict band to a payout split, every timeout/deadline comparison, input validation, storage writes, event/activity logging, output sanitization (clamping, key normalization). The model is only ever asked "what does the evidence show" — never "who should get paid" or "what should happen now."

## 3. Equivalence principles (full prose)

**Condition adjudication** uses `prompt_comparative` (never `prompt_non_comparative` — this decides a payout, so validators must independently re-derive the judgment, not just check the leader's output shape):

> "Both results are JSON verdicts about whether a physical item's delivered condition matches its promised listing condition, based on the same listing photo and delivery photo. Treat them as equivalent if they agree on the 'band' value (MATCH, MINOR_DISCREPANCY, MAJOR_DISCREPANCY, or NOT_RECEIVED) AND, when the band is MINOR_DISCREPANCY, their 'seller_payout_bps' values are within 1500 (15 percentage points) of each other. Differences in wording of 'reasoning', which specific discrepancy is named first, formatting, or key order are irrelevant. A different band, or a materially different payout split, is NOT equivalent."

Numeric output is banded (a coarse enum plus a bps split rounded to the nearest 500 by the contract before comparison) rather than a raw float, so validators compare a category, not a float — this is the "confidence HIGH not 0.83" pattern.

**Delivery-confirmation fetch** uses `prompt_comparative`:

> "Both results are JSON classifications of the same carrier tracking page into exactly one of DELIVERED, IN_TRANSIT, EXCEPTION, or UNKNOWN. Treat them as equivalent if and only if they name the same status. Reasoning wording is irrelevant."

**Contest re-adjudication** reuses the condition-adjudication principle verbatim (same equivalence rule — a contest still resolves to one of the same four bands) but leader/validators run a distinct adversarial prompt that explicitly instructs the model to look for reasons the first verdict might be wrong, so a contest is not degenerate re-confirmation of the same reasoning path.

`strict_eq` is not used anywhere — nothing in this contract produces byte-identical output validators could reproduce deterministically; every nondet block ends in a model judgment.

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
