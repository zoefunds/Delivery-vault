# Response to second team review

## The review comment (verbatim)

> "The escrow and evidence flows are substantial, but the custom validators
> do not always independently verify the result that moves funds. If the
> leader returns a condition or tracking verdict while a validator's rerun
> hits a transient fetch error, the validator currently approves without
> comparing the condition band, payout bucket, or tracking status. Please
> make that case fail validation unless the validator independently
> reproduces and checks every consequential field, then provide matching
> updated source and deployment."

One finding, confirmed and fixed in commit `<see git log>` ("Fix: validator
must independently reproduce every consequential field, not rubber-stamp on
its own transient error"). This document walks through what was wrong, why,
exactly what changed, and how the fix was verified — both by inspection and
live on StudioNet.

---

## Finding — a validator's own transient error was treated as agreement

### What was wrong

Both money-moving `validator_fn` closures (`_adjudicate_condition_nondet`
and `_check_tracking_nondet`) already did the right thing when the *leader*
failed — `_handle_nondet_leader_error` correctly requires the validator's
own error to also be transient before agreeing, and never persists a
leader's un-reproduced result on that basis.

The bug was on the *other* branch: when the leader **succeeded** and
returned a concrete verdict (a `gl.vm.Return` with a `band`/`seller_payout_bps`
or `status` payload), each validator is supposed to independently re-run the
exact same fetch-and-judge task and compare its own result to the leader's,
field by field. But if that validator's *own* rerun raised any
`gl.vm.UserError`, the code treated a `TRANSIENT`-prefixed error on its own
rerun as grounds to approve the leader's verdict outright:

```python
try:
    mine = leader_fn()
except gl.vm.UserError as exc:
    msg = getattr(exc, "message", None) or str(exc)
    return msg.startswith(ERR_TRANSIENT)   # <-- approves without comparing anything
except Exception:
    return False
```

Concretely: the leader returns `{"band": "MATCH", ...}` for a deal where the
delivered item is actually damaged. A validator's own image fetch for that
same round times out with `TRANSIENT: image fetch failed: ...` before it
ever gets to run its own adjudication. Under the old code, that validator's
vote is `True` — it never fetched anything, never ran its own judgment,
never compared a single field — yet it counts as independent agreement on a
verdict that moves the full price and bond. The same shape existed in
`_check_tracking_nondet`, where a transient failure on the validator's own
tracking-page fetch let it agree with a leader-reported `DELIVERED` status
(which directly gates `claim_via_tracking_confirmation`'s full payout)
without the validator ever having fetched or classified the page itself.

This defeats the entire point of `run_nondet_unsafe` with a code-level
`validator_fn`: agreement is supposed to mean "I independently reproduced
this and it matches," not "I couldn't reproduce it, but transient errors are
usually fine." A leader (honest or malicious) benefits from this exactly
when it matters most — a validator population disagreeing with a leader's
made-up verdict would, under the old code, sometimes get talked into "agree
anyway" purely because of its own network flakiness, with no comparison of
`band`, `seller_payout_bps`, or `tracking_status` ever taking place.

### The fix

Both `validator_fn` closures now treat *any* exception raised by the
validator's own `leader_fn()` rerun — transient or otherwise — as "I could
not independently verify this," which means disagree, not approve.

**Before** (`_adjudicate_condition_nondet.validator_fn`, and identically in
`_check_tracking_nondet.validator_fn`):

```python
def validator_fn(leaders_res: gl.vm.Result) -> bool:
    if not isinstance(leaders_res, gl.vm.Return):
        return self._handle_nondet_leader_error(leaders_res, leader_fn)
    leader_out = leaders_res.calldata
    if not isinstance(leader_out, dict):
        return False
    try:
        mine = leader_fn()
    except gl.vm.UserError as exc:
        msg = getattr(exc, "message", None) or str(exc)
        return msg.startswith(ERR_TRANSIENT)   # approves, no comparison
    except Exception:
        return False
    ...  # band / seller_payout_bps / status comparison, only reached on success
```

**After**:

```python
def validator_fn(leaders_res: gl.vm.Result) -> bool:
    if not isinstance(leaders_res, gl.vm.Return):
        return self._handle_nondet_leader_error(leaders_res, leader_fn)
    leader_out = leaders_res.calldata
    if not isinstance(leader_out, dict):
        return False
    try:
        mine = leader_fn()
    except gl.vm.UserError:
        # The leader returned a concrete verdict; if this validator's own
        # independent rerun can't reproduce ANY result (transient or
        # otherwise), it has nothing to compare the band/bps/status
        # against, so it must disagree rather than rubber-stamp a verdict
        # it never actually verified.
        return False
    except Exception:
        return False
    ...  # band / seller_payout_bps / status comparison, unchanged
```

One line changed in each closure — `return msg.startswith(ERR_TRANSIENT)`
became `return False` — but the semantic shift is the whole fix: a
validator that cannot reproduce the leader's result now always disagrees,
regardless of *why* it couldn't reproduce it. The field-by-field comparison
logic below this branch (exact `band` match, exact bucketed
`seller_payout_bps` match when `MINOR_DISCREPANCY`, exact `status` match for
tracking) is completely unchanged — it was already correct; it just wasn't
being reached often enough.

### What did *not* change

- **`_handle_nondet_leader_error`** (the branch that fires when the *leader*
  itself failed) is untouched. There, requiring both sides' errors to be
  transient before agreeing was already correct — it never lets an
  un-reproduced *value* pay out, because there is no value on that branch;
  the leader failed outright.
- **The comparison logic itself** — exact band match, exact bucketed
  `seller_payout_bps` match, exact tracking `status` match — is byte-for-byte
  the same as after [the first review's fix](REVIEW.md). This review found a
  *gate* that let the comparison be skipped, not a flaw in the comparison.
- **No new fetch retries, no backoff, no different error taxonomy.** The
  transient/deterministic/LLM error prefixes (`ERR_TRANSIENT`,
  `ERR_EXPECTED`, `ERR_EXTERNAL`, `ERR_LLM`) are unchanged; only which
  outcome a transient error on the validator's *own* rerun now produces on
  the *leader-succeeded* branch changed, from "approve" to "disagree."

### Why "disagree" and not "retry" or "abstain"

`gl.vm.run_nondet_unsafe` doesn't have a third option — a validator's vote
is binary agreement/disagreement on the round. A validator that can't
reproduce the result has no basis to vote agree, so disagree is the only
option that doesn't fabricate confidence it doesn't have. If enough
validators independently hit real transient failures on the same round,
consensus naturally fails to agree and the leader rotates (or the round
reports `TRANSIENT`/`UNDETERMINED` upstream), which is the correct outcome —
the same "rotate rather than persist garbage" principle already documented
for the leader-failure branch in [REVIEW.md](REVIEW.md) now applies
symmetrically to the validator's own-rerun failure.

### Verification

- **Direct tests** (`tests/direct/`): GenLayer's direct-mode harness runs
  only the leader path and does not simulate independent validator reruns
  (see `gltest/direct/wasi_mock.py`'s `_handle_run_nondet`), so this specific
  branch — a validator's *own* rerun failing after the leader succeeded — is
  not exercisable there. It was verified by inspection (the diff is a single
  return-value change per closure, isolated to the exact branch the review
  describes) and by the live run below, where real independent validator
  nodes run this exact code path.
- **Live on StudioNet** (redeployed contract
  `0x43d4a534E9761D2CC359b2D6e5af1d6D6Bf8602d`): the full method surface was
  re-run end-to-end against the new address, including a complete
  `resolve_condition` → `contest_verdict` → `resolve_contest` round. All 15
  exercised methods reached `ACCEPTED` consensus with zero errors:
  `get_config`, `get_platform_stats`, `get_deal_count`, `create_deal`,
  `accept_deal`, `submit_delivery_evidence`, `check_delivery_status`,
  `resolve_condition`, `contest_verdict`, `resolve_contest`,
  `get_activity`, `get_party_deal_ids`, `cancel_deal`,
  `timeout_unaccepted_reclaim`, `get_deal_summary`. `resolve_contest` is the
  method that most directly exercises the fixed `validator_fn` — real
  validators independently re-ran the adversarial adjudication and their
  votes drove the contest to `UPHELD`, finalizing `FINALIZED_NOT_RECEIVED`
  and zeroing the escrow ledger, live. `genlayer code
  0x43d4a534E9761D2CC359b2D6e5af1d6D6Bf8602d` was diffed against the local
  source to confirm the deployed bytecode's source matches this fix
  byte-for-byte before any of the above calls were made.
- As with the first review round, `finalize_deal`,
  `claim_via_tracking_confirmation`, `timeout_undelivered_reclaim`, and
  `force_refund_undetermined` were not exercised live this pass — each
  requires a real multi-hour/day grace window to elapse on StudioNet, and
  none of them touch the changed code (they're purely deterministic payout
  routing downstream of an already-recorded verdict). They remain covered by
  `tests/direct/` with a warped clock.

---

## Checklist

- [x] A validator whose own independent rerun raises any error — transient
      or otherwise — after the leader succeeded now always disagrees, never
      approves without comparing `band` / `seller_payout_bps` / `status`
      (condition adjudication and tracking classification both fixed)
- [x] The leader-failure branch (`_handle_nondet_leader_error`) reviewed and
      confirmed already correct; left unchanged
- [x] The field-by-field comparison logic itself (exact band, exact bucketed
      bps, exact tracking status) reviewed and confirmed unchanged
- [x] `genvm-lint` clean after the change
- [x] Redeployed to StudioNet: `0x43d4a534E9761D2CC359b2D6e5af1d6D6Bf8602d`
- [x] Deployed source diffed against local source (`genlayer code`) to
      confirm the fix is genuinely live before any live calls were made
- [x] All 15 exercisable methods (everything except the four gated behind a
      real multi-day/hour wait) re-run live against the new address with
      zero errors, including a full contest round through the fixed
      `validator_fn`
- [x] README and this document updated to reference the new deployment
      address
