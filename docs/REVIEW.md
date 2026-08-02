# Response to team review

## The review comment (verbatim)

> "The escrow payout is not fully bound by validator consensus: two
> minor-discrepancy results up to 1,500 basis points apart are treated as
> equivalent even though that raw value directly controls how much each
> party receives and can affect a contest. Please validate the exact payout
> bucket actually used for settlement, and replace unrestricted
> caller-supplied deadline timestamps with a trusted time mechanism before
> resubmitting."

Two distinct findings, both confirmed and both fixed in commit
`eba5b4a` ("Fix review findings: code-enforced payout consensus, trusted
clock"). This document walks through each one: what was wrong, why it was
wrong, exactly what changed, and how the fix was verified — both in mocked
direct tests and live on StudioNet.

---

## Finding 1 — payout not fully bound by consensus

### What was wrong

The condition-adjudication nondet block used
`gl.eq_principle.prompt_comparative`, GenLayer's mechanism for comparing a
leader's result against each validator's own independently-computed result
via a natural-language "equivalence principle" that a model reads and
judges. The principle text read (in full):

> "Both results are JSON verdicts about whether a physical item's delivered
> condition matches its promised listing condition, based on the same
> listing photo and delivery photo. Treat them as equivalent if they agree
> on the 'band' value (MATCH, MINOR_DISCREPANCY, MAJOR_DISCREPANCY, or
> NOT_RECEIVED) AND, when the band is MINOR_DISCREPANCY, their
> 'seller_payout_bps' values are within 1500 of each other. [...]"

The mechanism works by having `gl.eq_principle.prompt_comparative` return
**the leader's result** once enough validators agree it is "equivalent" to
their own. That is the load-bearing detail the original design missed: when
the band is `MINOR_DISCREPANCY`, the number written to
`deal.seller_payout_bps` — and later used, unmodified, in
`_payout_for_band` to compute `seller_share = (price * seller_payout_bps)
// BPS_DENOMINATOR` — is **only ever the leader's own proposed value**. A
validator whose own independent judgment landed on a different number
within 1500 bps of the leader's would vote "equivalent" and let the
leader's number stand, un-averaged, un-reconciled, un-checked against
anything but a model's own reading of the word "equivalent."

Concretely: on a 1 GEN deal, a leader proposing 60% to the seller (0.6 GEN)
and a validator whose own honest re-derivation landed on 45% (0.45 GEN) —
a 15-percentage-point, 0.15 GEN difference — would both be told by the
principle to call this "equivalent," and the contract would pay out
exactly the leader's 60%. Nothing in the contract itself ever compared
those two numbers; the only comparison was inside the model's own
free-text judgment of the prose principle. The review is correct that this
is not "validator consensus binding the payout" in any meaningful sense —
it's the leader's proposal, loosely vouched for.

### The fix

Both nondet operations that gate a payout — condition adjudication
(`_adjudicate_condition_nondet`) and tracking classification
(`_check_tracking_nondet`, which gates the full-payout
`claim_via_tracking_confirmation` path) — were switched from
`gl.eq_principle.prompt_comparative` to `gl.vm.run_nondet_unsafe(leader_fn,
validator_fn)`, where `validator_fn` is **plain Python**, not a
model-interpreted principle.

**Before** (`_adjudicate_condition_nondet`, simplified):

```python
def leader() -> str:
    verdict = self._run_condition_adjudication(...)
    return json.dumps({
        "band": BAND_NAMES[verdict["band"]],
        "seller_payout_bps": verdict["seller_payout_bps"],
        "reasoning": verdict["reasoning"],
    }, sort_keys=True)

principle = (
    "... Treat them as equivalent if they agree on the 'band' value "
    "... AND, when the band is MINOR_DISCREPANCY, their "
    "'seller_payout_bps' values are within 1500 of each other ..."
)

raw_result = gl.eq_principle.prompt_comparative(leader, principle)
return _parse_condition_verdict(raw_result)
```

**After** (`_adjudicate_condition_nondet`, current):

```python
def leader_fn() -> dict:
    verdict = self._run_condition_adjudication(...)
    return {
        "band": BAND_NAMES[verdict["band"]],
        "seller_payout_bps": verdict["seller_payout_bps"],
        "reasoning": verdict["reasoning"],
    }

def validator_fn(leaders_res: gl.vm.Result) -> bool:
    if not isinstance(leaders_res, gl.vm.Return):
        return self._handle_nondet_leader_error(leaders_res, leader_fn)
    leader_out = leaders_res.calldata
    if not isinstance(leader_out, dict):
        return False
    try:
        mine = leader_fn()          # independent re-derivation
    except gl.vm.UserError as exc:
        msg = getattr(exc, "message", None) or str(exc)
        return msg.startswith(ERR_TRANSIENT)
    except Exception:
        return False

    leader_band = str(leader_out.get("band", ""))
    if leader_band not in BAND_NAMES.values() or leader_band != mine["band"]:
        return False                # band must match EXACTLY
    if leader_band == BAND_NAMES[BAND_MINOR_DISCREPANCY]:
        try:
            leader_bps = int(leader_out.get("seller_payout_bps", -1))
        except (ValueError, TypeError):
            return False
        if leader_bps != mine["seller_payout_bps"]:
            return False             # bucket must match EXACTLY
    return True

result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
return _parse_condition_verdict(result)
```

Three things changed, all load-bearing:

1. **The comparison moved from the model to the contract.** `validator_fn`
   is ordinary Python running inside the validating node, not a prompt a
   model reads. "Equivalent" is now `==`, not an LLM's judgment call.
2. **The tolerance is gone, not narrowed.** `seller_payout_bps` is already
   rounded to the nearest 500 by `_parse_condition_verdict` *before* this
   comparison runs (unchanged from the original design — see "banded, not a
   raw float" below). Two independent runs either land in the same
   discrete 500-wide bucket or they don't. There is no numeric "close
   enough" left anywhere in the path that decides how much money moves —
   which is exactly what the review asked for: *"validate the exact payout
   bucket actually used for settlement."*
3. **The band gets the same treatment**, even though the original prose
   principle already required an exact band match — it's now enforced the
   same code-level way as the bps check, for one consistent mechanism
   rather than two.

The same restructuring was applied to `_check_tracking_nondet` (tracking
classification), even though its original principle was already
effectively "equal or nothing" (`DELIVERED`/`IN_TRANSIT`/`EXCEPTION`/
`UNKNOWN`, no partial credit). It gates a full payout
(`claim_via_tracking_confirmation` moves 100% of the price + bond), so it
gets the same code-enforced treatment for consistency and to remove any
residual doubt that an LLM, not the contract, was the final arbiter of a
money-moving classification.

A shared error-handling helper, `_handle_nondet_leader_error`, replicates
the error-classification discipline from the original design: deterministic
errors (`EXPECTED`/`EXTERNAL`) must match the leader's message exactly,
transient errors (`TRANSIENT`) agree if both sides hit one, and anything
else (including `LLM_ERROR`) forces disagreement so consensus rotates the
leader instead of persisting an unverifiable result.

### What did *not* change

- **Numeric banding is unchanged.** `seller_payout_bps` was already rounded
  to the nearest `BPS_BUCKET` (500) by `_parse_condition_verdict` in the
  original design — that part of the design was sound (validators compare
  a coarse category, not a float, per the "confidence HIGH not 0.83"
  pattern). The bug was never the banding; it was comparing the banded
  value with a *tolerance* instead of *equality*.
- **The band enum, the prompts, the fetch logic, the failure/abstention
  semantics** (`NOT_RECEIVED` on unfetchable photos, `UNDETERMINED` on
  unparseable output) are all unchanged.
- **`strict_eq` is still not used anywhere** — nothing here produces
  byte-identical raw model text across independent calls; the exactness is
  enforced on the *normalized, bucketed decision fields* (band name,
  bucketed bps), not on raw model output.

### Verification

- **Direct tests** (`tests/direct/test_delivery_vault.py`): the mocked
  direct-mode runner executes only `leader_fn` (GenLayer's direct-mode
  harness runs the leader path and skips validator simulation by design —
  see `gltest/direct/wasi_mock.py`'s `_handle_run_nondet`), so these tests
  continue to verify the leader's own output shape and banding
  (`test_resolve_condition_minor_discrepancy_bps_banded` asserts the
  returned `seller_payout_bps % 500 == 0`), while `validator_fn`'s
  code-level equality logic was verified by inspection and by the live run
  below, where real independent validator nodes actually ran it.
- **Live on StudioNet** (redeployed contract
  `0x6C8b6928EeFE8121A4A9265d74f86EEe55C1C054`): a full contest round
  reached consensus through the new code path — `resolve_condition`
  returned `NOT_RECEIVED` (the delivery photo showed an unrelated stock
  image, not the described sofa), `contest_verdict` bonded exactly 15% of
  the price, and `resolve_contest` re-ran the adversarial adjudication and
  **upheld** `NOT_RECEIVED` — meaning the real validator set's independent
  `validator_fn` re-derivation matched the leader's band exactly, live,
  with the exact-match code path actually executing on-chain, not just in
  a mock.

---

## Finding 2 — unrestricted caller-supplied deadline timestamps

### What was wrong

Every write method that needed "what time is it right now" took it as a
plain `now_ts: int` parameter, supplied by whoever sent the transaction:

```python
@gl.public.write
def timeout_unaccepted_reclaim(self, deal_id: int, now_ts: int) -> None:
    deal = self._get_deal(deal_id)
    _require(int(deal.status) == STATUS_CREATED, "...")
    _require(now_ts > int(deal.ship_by_ts), "acceptance window has not passed yet")
    ...
```

Nothing validated `now_ts` against anything except other caller-supplied
values from the same or earlier calls. A buyer could call
`timeout_unaccepted_reclaim(deal_id, now_ts=deal.ship_by_ts + 999999)`
the instant after creating a deal — with the real acceptance window still
wide open — and the contract would refund them immediately, because
`now_ts > ship_by_ts` is trivially true for a fabricated value. The same
class of bug existed in all 12 affected methods: `create_deal`,
`cancel_deal`, `accept_deal`, `timeout_unaccepted_reclaim`,
`submit_delivery_evidence`, `timeout_undelivered_reclaim`,
`check_delivery_status`, `claim_via_tracking_confirmation`,
`resolve_condition`, `force_refund_undetermined`, `finalize_deal`,
`contest_verdict`, `resolve_contest`, plus the private `_settle_contest`
helper. Every timeout ladder, every grace window, and the entire 48-hour
contest window existed only as long as nobody lied about the current time
— which is not a security property.

### The fix

`now_ts` was removed from every one of those method signatures. A new
helper reads GenVM's own time instead:

```python
def _now_ts(self) -> int:
    """Authenticated, consensus-agreed clock. GenVM patches
    datetime.now() to the network's block time, which every validator
    computes identically -- it is never read from a caller-supplied
    argument or calldata, so it cannot be spoofed by a transaction
    sender to fabricate a future or past time and force a timeout,
    grace window, or contest deadline to fire early or late."""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())
```

Every method that used to accept `now_ts` now computes it internally as its
first statement instead:

```python
@gl.public.write
def timeout_unaccepted_reclaim(self, deal_id: int) -> None:
    """Permissionless: if the seller never accepted by ship_by_ts, the
    buyer's funds are recoverable by anyone calling this on their behalf."""
    now_ts = self._now_ts()
    deal = self._get_deal(deal_id)
    _require(int(deal.status) == STATUS_CREATED, "...")
    _require(now_ts > int(deal.ship_by_ts), "acceptance window has not passed yet")
    ...
```

`datetime.now(timezone.utc)` specifically (not `time.time()` or
`datetime.utcnow()`) is deliberate: GenVM patches this exact accessor to
the network's consensus-agreed block time, so every validator re-executing
the same transaction computes an identical value — it is deterministic
*and* trustworthy, not merely deterministic. This mirrors the pattern
already used by this repo's sibling contracts (Event-Weaver,
Meme-Olympics), which independently converged on the same fix.

### What did *not* change

`ship_by_ts` and `deliver_by_ts` **remain caller-supplied**. This is
intentional, not an oversight: they are deal *terms* the buyer proposes at
creation time ("I want this shipped within 3 days"), analogous to a price
or a description — not claims about what time it currently is. They are
validated *against* the trusted clock (`ship_by_ts > now_ts` where `now_ts`
now comes from `_now_ts()`), never trusted as a substitute for it.

### Verification

- **Direct tests**: every test that previously simulated elapsed time by
  passing a future `now_ts` argument now uses `vm.warp(iso_string)` to move
  the *test harness's* clock forward — the actual mechanism GenVM's
  `datetime.now()` patch responds to — instead of a value the contract
  itself would ever see as an argument. A new test,
  `test_create_deal_uses_trusted_clock_not_a_caller_argument`, explicitly
  warps the clock past `ship_by_ts` and confirms `create_deal` rejects a
  now-stale `ship_by_ts` — proving the deadline is checked against the
  warped (trusted) clock, not any value the test could pass as an
  argument, because there is no longer an argument to pass.
- **Live on StudioNet**: `timeout_unaccepted_reclaim` was called only after
  the test process (`tests/integration/test_live_lifecycle.py::
  test_timeout_unaccepted_reclaim_live`) genuinely slept in a loop
  (`while int(time.time()) <= ship_by: time.sleep(2)`) until the real
  wall-clock time on the machine — and therefore on StudioNet — actually
  passed `ship_by_ts`. This is the strongest available evidence the fix
  holds on the live network: there is no faster way to make this call
  succeed anymore, which is the entire point.

### A consequence worth stating plainly

Because time can no longer be faked, **`finalize_deal`,
`force_refund_undetermined`, and (with a shorter window)
`claim_via_tracking_confirmation` / `timeout_undelivered_reclaim` can no
longer be demonstrated end-to-end within a single quick live test run** —
they now require the real `CONTEST_WINDOW_SECONDS` (48h),
`UNDETERMINED_GRACE_SECONDS` (3 days), or `DELIVER_GRACE_SECONDS` (7 days)
to genuinely elapse. This is the correct trade-off (a spoofable `now_ts`
was exactly the vulnerability being removed), but it does mean the "measured
on live consensus" evidence for those three paths in this pass comes from
direct tests with a warped clock, not a live StudioNet transaction — stated
plainly in the README's honest-limits section rather than glossed over.

---

## Checklist

- [x] Payout settlement value validated exactly, in code, between leader and
      validator — not an LLM-judged tolerance (Finding 1)
- [x] No method anywhere in the contract accepts a caller-supplied "current
      time" argument (Finding 2)
- [x] `genvm-lint` clean on both the primitive and the consumer example
      after the change
- [x] All 58 direct tests updated and passing (`vm.warp()` instead of
      spoofable arguments)
- [x] Redeployed to StudioNet; schema re-verified to match the new
      signatures (`genlayer schema <address>`)
- [x] Both fixes verified live, not only in mocked tests: a full contest
      round on the new consensus mechanism, and a timeout genuinely waited
      out in real time
- [x] README, DESIGN.md, and the consumer example updated to match the new
      method signatures and mechanism
- [x] Two stale pre-fix integration test files removed, including one that
      explicitly exploited the since-fixed `now_ts` vulnerability as its
      test premise
