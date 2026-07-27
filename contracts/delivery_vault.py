# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import re
from dataclasses import dataclass

from genlayer import *


# ============================================================================
#  Physical-Goods Delivery-Condition Vault
#
#  Escrow primitive for peer-to-peer physical-goods trades. A buyer funds the
#  agreed price; the seller optionally posts a performance bond; validators
#  independently fetch the listing photo and the arrival photo (contract-side,
#  never trusted from calldata) and judge whether the delivered condition
#  matches what was promised. A separate, independent web-fetch of the
#  carrier's tracking page lets either party demonstrate delivery even if the
#  other side goes silent. Either counterparty may bond a single contest to
#  force a second, adversarially-framed judgment before funds finalize.
# ============================================================================


# ----------------------------------------------------------------------------
# Error classification prefixes - deterministic, machine-parseable.
# ----------------------------------------------------------------------------
ERR_EXPECTED = "EXPECTED: "    # caller mistake / wrong state
ERR_EXTERNAL = "EXTERNAL: "    # upstream 4xx-style failure
ERR_TRANSIENT = "TRANSIENT: "  # network/5xx flakiness - retryable
ERR_LLM = "LLM_ERROR: "        # model output unusable after sanitation


# ----------------------------------------------------------------------------
# Deal lifecycle statuses.
# ----------------------------------------------------------------------------
STATUS_CREATED = 0             # buyer funded, awaiting seller acceptance
STATUS_ACCEPTED = 1            # seller accepted; awaiting delivery evidence
STATUS_DELIVERY_SUBMITTED = 2  # buyer submitted arrival photo; awaiting adjudication
STATUS_UNDETERMINED = 3        # adjudication ran but was inconclusive; retryable
STATUS_VERDICT_PENDING = 4     # verdict recorded; contest window open, funds still escrowed
STATUS_CONTESTED = 5           # a contest bond is posted; second adjudication pending
STATUS_FINALIZED_MATCH = 6
STATUS_FINALIZED_MINOR = 7
STATUS_FINALIZED_MAJOR = 8
STATUS_FINALIZED_NOT_RECEIVED = 9
STATUS_FINALIZED_TRACKING_CLAIM = 10   # seller claimed via tracking proof, buyer never disputed
STATUS_CANCELLED = 11                  # buyer cancelled pre-acceptance
STATUS_TIMEOUT_UNACCEPTED = 12         # seller never accepted in time; buyer reclaimed
STATUS_TIMEOUT_UNDELIVERED = 13        # buyer never submitted evidence; buyer reclaimed
STATUS_TIMEOUT_UNDETERMINED_REFUND = 14  # adjudication never converged; buyer refunded

STATUS_NAMES = {
    STATUS_CREATED: "CREATED",
    STATUS_ACCEPTED: "ACCEPTED",
    STATUS_DELIVERY_SUBMITTED: "DELIVERY_SUBMITTED",
    STATUS_UNDETERMINED: "UNDETERMINED",
    STATUS_VERDICT_PENDING: "VERDICT_PENDING",
    STATUS_CONTESTED: "CONTESTED",
    STATUS_FINALIZED_MATCH: "FINALIZED_MATCH",
    STATUS_FINALIZED_MINOR: "FINALIZED_MINOR_DISCREPANCY",
    STATUS_FINALIZED_MAJOR: "FINALIZED_MAJOR_DISCREPANCY",
    STATUS_FINALIZED_NOT_RECEIVED: "FINALIZED_NOT_RECEIVED",
    STATUS_FINALIZED_TRACKING_CLAIM: "FINALIZED_TRACKING_CLAIM",
    STATUS_CANCELLED: "CANCELLED",
    STATUS_TIMEOUT_UNACCEPTED: "TIMEOUT_UNACCEPTED",
    STATUS_TIMEOUT_UNDELIVERED: "TIMEOUT_UNDELIVERED",
    STATUS_TIMEOUT_UNDETERMINED_REFUND: "TIMEOUT_UNDETERMINED_REFUND",
}

# Condition-verdict bands. NONE means "no verdict recorded yet".
BAND_NONE = 0
BAND_MATCH = 1
BAND_MINOR_DISCREPANCY = 2
BAND_MAJOR_DISCREPANCY = 3
BAND_NOT_RECEIVED = 4

BAND_NAMES = {
    BAND_NONE: "NONE",
    BAND_MATCH: "MATCH",
    BAND_MINOR_DISCREPANCY: "MINOR_DISCREPANCY",
    BAND_MAJOR_DISCREPANCY: "MAJOR_DISCREPANCY",
    BAND_NOT_RECEIVED: "NOT_RECEIVED",
}
BAND_FROM_NAME = {v: k for k, v in BAND_NAMES.items()}

# Tracking-page classification.
TRACK_UNKNOWN = 0
TRACK_IN_TRANSIT = 1
TRACK_DELIVERED = 2
TRACK_EXCEPTION = 3

TRACK_NAMES = {
    TRACK_UNKNOWN: "UNKNOWN",
    TRACK_IN_TRANSIT: "IN_TRANSIT",
    TRACK_DELIVERED: "DELIVERED",
    TRACK_EXCEPTION: "EXCEPTION",
}
TRACK_FROM_NAME = {v: k for k, v in TRACK_NAMES.items()}

# ----------------------------------------------------------------------------
# Hard limits - sanity rails.
# ----------------------------------------------------------------------------
MAX_TITLE_LEN = 160
MAX_DESCRIPTION_LEN = 2000
MAX_URL_LEN = 500
MAX_REASONING_STORED = 1200
MAX_EVIDENCE_EXCERPT = 6000       # chars of rendered tracking page fed to the LLM
MAX_ACTIVITY_NOTE_LEN = 200

BPS_DENOMINATOR = 10000

# Contest bond is a fixed fraction of the price - immutable, no setter, so no
# privileged party can tune it in their own favor after a deal is live.
CONTEST_BOND_BPS = 1500  # 15% of price

# Timing constants, expressed in seconds and applied to caller-supplied unix
# timestamps (this chain has no trusted read-time clock the contract itself
# can consult mid-transaction; every deadline is computed from timestamps the
# caller supplies at creation/accept time and compared against later
# caller-supplied "now" values, exactly as validators re-derive them).
CONTEST_WINDOW_SECONDS = 172800          # 48h to contest a recorded verdict
SELLER_TRACKING_CLAIM_GRACE_SECONDS = 259200   # 3 days past deliver_by_ts
DELIVER_GRACE_SECONDS = 604800                 # 7 days past deliver_by_ts
UNDETERMINED_GRACE_SECONDS = 259200            # 3 days after last adjudication attempt
MAX_RESOLVE_ATTEMPTS = 5

# Minor-discrepancy payout bps is banded to the nearest 500 (5 percentage
# points) before comparison, so validators compare a coarse category rather
# than a raw float.
BPS_BUCKET = 500


# ============================================================================
#  Storage dataclasses - only str / u8 / u32 / u64 / u256 / bool fields.
# ============================================================================

@allow_storage
@dataclass
class Deal:
    id: u32
    buyer: Address
    seller: Address
    item_title: str
    item_description: str
    listing_photo_url: str
    tracking_url: str
    delivery_photo_url: str        # empty until buyer submits

    status: u8
    created_ts: u64
    ship_by_ts: u64                # seller must accept by this ts
    deliver_by_ts: u64             # buyer should submit evidence by this ts

    # Escrow ledger - "_wei" is the agreed term, "_deposited_wei" is the
    # actual custody balance. Every payout path reads only the "_deposited_wei"
    # field and zeroes it before transferring, so double-spend is structurally
    # impossible (a second call always finds the balance already at zero).
    price_wei: str
    price_deposited_wei: str
    seller_bond_wei: str
    seller_bond_deposited_wei: str
    contest_bond_wei: str
    contest_bond_deposited_wei: str
    contester: str                  # hex address of whoever contested, "" if none

    tracking_status: u8
    tracking_checked: bool

    verdict_band: u8
    seller_payout_bps: u32          # meaningful only when verdict_band == MINOR_DISCREPANCY
    verdict_reasoning: str

    final_band: u8                  # set once finalized (may differ from verdict_band if overturned)
    final_seller_payout_bps: u32
    contest_outcome: str             # "" | "UPHELD" | "OVERTURNED"

    resolve_attempts: u32
    contest_count: u32
    resolved_ts: u64                 # ts of the recorded verdict (starts contest window)
    finalized_ts: u64


@allow_storage
@dataclass
class ActivityEvent:
    kind: str
    actor: Address
    amount: u256
    ts: u64
    note: str


# ============================================================================
#  Pure helpers (deterministic - safe anywhere).
# ============================================================================

def _require(cond: bool, message: str) -> None:
    if not cond:
        raise gl.vm.UserError(ERR_EXPECTED + message)


def _clamp_int(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _validate_url(url: str, field: str) -> str:
    u = url.strip()
    _require(0 < len(u) <= MAX_URL_LEN, f"{field} must be 1..{MAX_URL_LEN} chars")
    _require(
        bool(re.match(r"^https?://[^\s]+\.[^\s]+", u)),
        f"{field} must be a valid http(s) URL",
    )
    return u


def _sanitize_json_text(text: str) -> str:
    """Strip markdown fences and leading/trailing chatter around a JSON object."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        stripped = stripped[start : end + 1]
    return stripped.strip()


def _parse_json_object(raw) -> dict:
    """Normalize an LLM response into a dict, or raise LLM_ERROR."""
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(_sanitize_json_text(payload))
        except (json.JSONDecodeError, ValueError):
            raise gl.vm.UserError(ERR_LLM + "response was not parseable JSON")
    if not isinstance(payload, dict):
        raise gl.vm.UserError(ERR_LLM + "response JSON was not an object")
    return payload


def _first_present(payload: dict, keys: list) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _coerce_int(value, lo: int, hi: int, field: str) -> int:
    if value is None:
        raise gl.vm.UserError(ERR_LLM + f"missing numeric field '{field}'")
    try:
        number = int(round(float(str(value).strip().rstrip("%"))))
    except (ValueError, TypeError):
        raise gl.vm.UserError(ERR_LLM + f"non-numeric field '{field}': {value}")
    return _clamp_int(number, lo, hi)


def _bucket_bps(value: int) -> int:
    """Round a 0..10000 bps value to the nearest BPS_BUCKET, so validators
    compare a coarse category rather than a raw integer."""
    v = _clamp_int(int(value), 0, BPS_DENOMINATOR)
    return int(round(v / BPS_BUCKET) * BPS_BUCKET)


def _parse_condition_verdict(raw) -> dict:
    """Normalize a condition-adjudication response into
    {band, seller_payout_bps, reasoning}. Tolerant of alias keys; raises
    LLM_ERROR only when the band itself is unrecoverable."""
    payload = _parse_json_object(raw)

    band_raw = _first_present(payload, ["band", "verdict", "verdict_band", "outcome"])
    band_name = str(band_raw).strip().upper() if band_raw is not None else ""
    band_name = band_name.replace(" ", "_").replace("-", "_")
    band = BAND_FROM_NAME.get(band_name)
    if band is None:
        raise gl.vm.UserError(ERR_LLM + f"unrecognized verdict band: {band_raw!r}")

    payout_raw = _first_present(
        payload, ["seller_payout_bps", "payout_bps", "seller_share_bps", "seller_bps"]
    )
    if band == BAND_MINOR_DISCREPANCY:
        payout_bps = _bucket_bps(_coerce_int(payout_raw, 1, 9999, "seller_payout_bps"))
    else:
        payout_bps = 0

    reasoning_raw = _first_present(payload, ["reasoning", "rationale", "explanation"])
    reasoning = str(reasoning_raw) if reasoning_raw is not None else ""

    return {
        "band": band,
        "seller_payout_bps": payout_bps,
        "reasoning": _truncate(reasoning, MAX_REASONING_STORED),
    }


def _parse_tracking_verdict(raw) -> dict:
    payload = _parse_json_object(raw)
    status_raw = _first_present(payload, ["status", "tracking_status", "verdict"])
    status_name = str(status_raw).strip().upper() if status_raw is not None else ""
    status_name = status_name.replace(" ", "_").replace("-", "_")
    status = TRACK_FROM_NAME.get(status_name, TRACK_UNKNOWN)
    reasoning_raw = _first_present(payload, ["reasoning", "rationale", "explanation"])
    reasoning = str(reasoning_raw) if reasoning_raw is not None else ""
    return {"status": status, "reasoning": _truncate(reasoning, MAX_REASONING_STORED)}


# ============================================================================
#  Native transfer target. Payouts go to plain wallets (EOAs), which requires
#  the EVM-compatibility interface - gl.get_contract_at(...).emit_transfer(...)
#  only works against another deployed Intelligent Contract and fails against
#  an address with no contract code, which is what every user's wallet is.
# ============================================================================

@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def _send_gen(to_address: Address, amount: int) -> None:
    """Single emission choke point for every native-token payout. Callers
    MUST zero and persist the ledger field(s) BEFORE calling this - never
    after - so a reentrant or duplicated call always finds the balance
    already at zero."""
    if amount <= 0:
        return
    _Recipient(to_address).emit_transfer(value=u256(int(amount)))


# ============================================================================
#  The Contract
# ============================================================================

class DeliveryVault(gl.Contract):
    """Escrow for peer-to-peer physical-goods trades, released only when
    validator consensus judges delivered condition against listing photos,
    with an independent web-fetched tracking-proof path and a bonded
    single-round contest ladder. No owner, no admin, no pause, no fee."""

    deal_count: u64
    deals: TreeMap[u32, Deal]
    activity: TreeMap[u32, DynArray[ActivityEvent]]

    # deals a given address is party to (as buyer or seller), for discovery.
    party_deal_ids: TreeMap[Address, DynArray[u32]]

    total_volume_wei: u256
    total_deals: u64
    total_finalized: u64
    total_contests: u64

    # ------------------------------------------------------------------
    #  Construction
    # ------------------------------------------------------------------

    def __init__(self):
        self.deal_count = u64(0)
        self.total_volume_wei = u256(0)
        self.total_deals = u64(0)
        self.total_finalized = u64(0)
        self.total_contests = u64(0)

    # ------------------------------------------------------------------
    #  Internal deterministic utilities
    # ------------------------------------------------------------------

    def _get_deal(self, deal_id: int) -> Deal:
        did = u32(deal_id)
        deal = self.deals.get(did)
        if deal is None:
            raise gl.vm.UserError(ERR_EXPECTED + f"deal {deal_id} does not exist")
        return deal

    def _log(self, deal_id: int, kind: str, actor: Address, amount: int, ts: int, note: str) -> None:
        did = u32(deal_id)
        if self.activity.get(did) is None:
            self.activity[did] = []
        self.activity[did].append(
            ActivityEvent(
                kind=kind,
                actor=actor,
                amount=u256(max(0, amount)),
                ts=u64(max(0, ts)),
                note=_truncate(note, MAX_ACTIVITY_NOTE_LEN),
            )
        )

    def _record_party(self, addr: Address, deal_id: int) -> None:
        if self.party_deal_ids.get(addr) is None:
            self.party_deal_ids[addr] = []
        arr = self.party_deal_ids[addr]
        did = u32(deal_id)
        for existing in arr:
            if existing == did:
                return
        arr.append(did)

    def _addr_eq(self, a: Address, b) -> bool:
        b_addr = b if isinstance(b, Address) else Address(b)
        return a == b_addr

    def _send_native(self, recipient: Address, amount: int) -> None:
        _send_gen(recipient, amount)

    # ------------------------------------------------------------------
    #  Condition adjudication - image evidence, comparative equivalence.
    # ------------------------------------------------------------------

    def _build_condition_prompt(
        self,
        item_title: str,
        item_description: str,
        tracking_note: str,
        adversarial: bool,
    ) -> str:
        stance = (
            "You are conducting a SECOND, independent review of a disputed "
            "verdict. Actively look for reasons the first verdict might be "
            "wrong - do not simply re-confirm an easy answer. Scrutinize "
            "wear, damage, missing parts, and staging differences closely."
            if adversarial
            else "You are the first neutral adjudicator for this trade."
        )
        return f"""{stance}

Two images are attached: image 1 is the SELLER'S LISTING PHOTO (promised
condition at time of listing). Image 2 is the BUYER'S DELIVERY PHOTO (actual
condition as received), or is absent if none could be fetched.

ITEM: {item_title}
DESCRIPTION: {item_description}
DELIVERY-TRACKING CONTEXT (informational only, do not treat as proof of
condition): {tracking_note}

Judge ONLY from what is visually depicted plus the text above. Classify into
exactly one band:
- "MATCH": the delivered item is the same item in materially the same
  condition as promised.
- "MINOR_DISCREPANCY": the item arrived, is recognizably the same item, but
  has a limited, non-disqualifying difference (light wear, a scuff, a missing
  minor accessory). Also provide seller_payout_bps: the percentage (0-10000
  basis points) of the price fair for the seller to keep, reflecting how
  minor the discrepancy is.
- "MAJOR_DISCREPANCY": the item is damaged, materially different, or
  substantially not as described.
- "NOT_RECEIVED": no delivery photo could be meaningfully evaluated (missing,
  blank, unrelated to the listing, or clearly not the purchased item at all).

Return ONLY a JSON object exactly like:
{{
  "band": "MATCH" | "MINOR_DISCREPANCY" | "MAJOR_DISCREPANCY" | "NOT_RECEIVED",
  "seller_payout_bps": 0,
  "reasoning": "one short paragraph"
}}"""

    def _run_condition_adjudication(
        self,
        listing_photo_url: str,
        delivery_photo_url: str,
        item_title: str,
        item_description: str,
        tracking_note: str,
        adversarial: bool,
    ) -> dict:
        """Shared leader/validator task. Runs INSIDE a nondet closure. All
        gl.nondet.* calls live directly in this method body (not in a
        further-nested helper) so they stay lexically reachable from the
        leader closure that calls this method."""
        images = []
        urls_to_fetch = [listing_photo_url]
        if delivery_photo_url:
            urls_to_fetch.append(delivery_photo_url)
        fetch_ok = []

        for url in urls_to_fetch:
            try:
                response = gl.nondet.web.get(url)
            except Exception as exc:  # noqa: BLE001 - network-layer failure
                raise gl.vm.UserError(ERR_TRANSIENT + f"image fetch failed: {exc}")
            status = getattr(response, "status", getattr(response, "status_code", 0))
            if status >= 500:
                raise gl.vm.UserError(ERR_TRANSIENT + f"HTTP {status} upstream error")
            body = getattr(response, "body", None)
            if 400 <= status < 500 or not isinstance(body, (bytes, bytearray)) or len(body) == 0:
                fetch_ok.append(False)
            else:
                fetch_ok.append(True)
                images.append(bytes(body))

        listing_ok = fetch_ok[0]
        delivery_ok = bool(delivery_photo_url) and len(fetch_ok) > 1 and fetch_ok[1]

        if not listing_ok or not delivery_ok:
            # A required photo could not be fetched at all (non-transient
            # failure). This is evidence of "nothing meaningful to judge",
            # never coerced into a damage verdict.
            return {
                "band": BAND_NOT_RECEIVED,
                "seller_payout_bps": 0,
                "reasoning": "Required photo evidence was not fetchable "
                "(listing_ok=%s, delivery_ok=%s)." % (listing_ok, delivery_ok),
            }

        prompt = self._build_condition_prompt(
            item_title, item_description, tracking_note, adversarial
        )
        try:
            if images:
                raw = gl.nondet.exec_prompt(prompt, response_format="json", images=images)
            else:
                raw = gl.nondet.exec_prompt(prompt, response_format="json")
        except Exception as exc:  # noqa: BLE001
            raise gl.vm.UserError(ERR_LLM + f"exec_prompt failed: {exc}")
        return _parse_condition_verdict(raw)

    def _adjudicate_condition_nondet(
        self,
        listing_photo_url: str,
        delivery_photo_url: str,
        item_title: str,
        item_description: str,
        tracking_note: str,
        adversarial: bool,
    ) -> dict:
        def leader() -> str:
            verdict = self._run_condition_adjudication(
                listing_photo_url,
                delivery_photo_url,
                item_title,
                item_description,
                tracking_note,
                adversarial,
            )
            return json.dumps(
                {
                    "band": BAND_NAMES[verdict["band"]],
                    "seller_payout_bps": verdict["seller_payout_bps"],
                    "reasoning": verdict["reasoning"],
                },
                sort_keys=True,
            )

        principle = (
            "Both results are JSON verdicts about whether a physical item's "
            "delivered condition matches its promised listing condition, "
            "based on the same listing photo and delivery photo. Treat them "
            "as equivalent if they agree on the 'band' value (MATCH, "
            "MINOR_DISCREPANCY, MAJOR_DISCREPANCY, or NOT_RECEIVED) AND, "
            "when the band is MINOR_DISCREPANCY, their 'seller_payout_bps' "
            "values are within 1500 of each other. Differences in wording of "
            "'reasoning', which discrepancy is named first, formatting, or "
            "key order are irrelevant. A different band, or a materially "
            "different payout split outside that tolerance, is NOT "
            "equivalent."
        )

        raw_result = gl.eq_principle.prompt_comparative(leader, principle)
        return _parse_condition_verdict(raw_result)

    # ------------------------------------------------------------------
    #  Tracking-page adjudication - independent web-fetch surface.
    # ------------------------------------------------------------------

    def _build_tracking_prompt(self, tracking_url: str, page_text: str) -> str:
        return f"""You are classifying a shipping carrier's tracking page into
exactly one delivery status. This is being used to help resolve a
buyer/seller dispute, so classify conservatively - only report DELIVERED if
the page unambiguously states the package was delivered.

TRACKING URL: {tracking_url}
PAGE TEXT (rendered, may be truncated):
{page_text[:MAX_EVIDENCE_EXCERPT]}

Classify into exactly one of:
- "DELIVERED": the page states the package was delivered.
- "IN_TRANSIT": the package is still moving through the carrier network.
- "EXCEPTION": the page indicates a problem (lost, returned, delayed
  indefinitely, delivery failed).
- "UNKNOWN": the page could not be understood, is empty, or does not
  clearly indicate any of the above.

Return ONLY a JSON object exactly like:
{{
  "status": "DELIVERED" | "IN_TRANSIT" | "EXCEPTION" | "UNKNOWN",
  "reasoning": "one short sentence"
}}"""

    def _run_tracking_check(self, tracking_url: str) -> dict:
        """Runs INSIDE a nondet closure - the fetch and the exec_prompt call
        both live directly in this method body, one hop from the leader."""
        text = None
        render = getattr(getattr(gl.nondet, "web", None), "render", None)
        if render is not None:
            try:
                text = str(render(tracking_url, mode="text"))
            except TypeError:
                text = str(render(tracking_url))
            except Exception as exc:  # noqa: BLE001
                raise gl.vm.UserError(ERR_TRANSIENT + f"tracking fetch failed: {exc}")
        else:
            try:
                response = gl.nondet.web.get(tracking_url)
            except Exception as exc:  # noqa: BLE001
                raise gl.vm.UserError(ERR_TRANSIENT + f"tracking fetch failed: {exc}")
            body = getattr(response, "body", b"")
            text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)

        if not text:
            return {"status": TRACK_UNKNOWN, "reasoning": "tracking page not fetchable"}

        prompt = self._build_tracking_prompt(tracking_url, text)
        try:
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
        except Exception as exc:  # noqa: BLE001
            raise gl.vm.UserError(ERR_LLM + f"exec_prompt failed: {exc}")
        return _parse_tracking_verdict(raw)

    def _check_tracking_nondet(self, tracking_url: str) -> dict:
        def leader() -> str:
            verdict = self._run_tracking_check(tracking_url)
            return json.dumps(
                {
                    "status": TRACK_NAMES[verdict["status"]],
                    "reasoning": verdict["reasoning"],
                },
                sort_keys=True,
            )

        principle = (
            "Both results are JSON classifications of the same carrier "
            "tracking page into exactly one of DELIVERED, IN_TRANSIT, "
            "EXCEPTION, or UNKNOWN. Treat them as equivalent if and only if "
            "they name the same status. Reasoning wording is irrelevant."
        )

        raw_result = gl.eq_principle.prompt_comparative(leader, principle)
        return _parse_tracking_verdict(raw_result)

    # ------------------------------------------------------------------
    #  Deterministic payout routing (no nondet calls below this line).
    # ------------------------------------------------------------------

    def _payout_for_band(self, deal: Deal, band: int, seller_payout_bps: int) -> None:
        """Zero the escrow ledger fields, persist, THEN transfer - checks-
        effects-interactions, so a duplicated call always finds the ledger
        already at zero before ever reaching a transfer."""
        price = int(deal.price_deposited_wei)
        bond = int(deal.seller_bond_deposited_wei)
        deal.price_deposited_wei = "0"
        deal.seller_bond_deposited_wei = "0"

        if band == BAND_MATCH:
            self._send_native(deal.seller, price + bond)
        elif band == BAND_MAJOR_DISCREPANCY or band == BAND_NOT_RECEIVED:
            self._send_native(deal.buyer, price + bond)
        elif band == BAND_MINOR_DISCREPANCY:
            seller_share = (price * seller_payout_bps) // BPS_DENOMINATOR
            buyer_share = price - seller_share
            if seller_share + bond > 0:
                self._send_native(deal.seller, seller_share + bond)
            if buyer_share > 0:
                self._send_native(deal.buyer, buyer_share)
        else:
            raise gl.vm.UserError(ERR_EXPECTED + f"unhandled band {band}")

    # ========================================================================
    #  PUBLIC WRITES - deal lifecycle (deterministic)
    # ========================================================================

    @gl.public.write.payable
    def create_deal(
        self,
        seller: str,
        item_title: str,
        item_description: str,
        listing_photo_url: str,
        tracking_url: str,
        ship_by_ts: int,
        deliver_by_ts: int,
        now_ts: int,
    ) -> int:
        """Buyer creates and funds a deal. Attach the full agreed price as
        native value.

        Args:
            seller: hex address of the counterparty.
            item_title / item_description: what is being sold.
            listing_photo_url: the seller's promised-condition photo.
            tracking_url: carrier tracking page for this shipment (required -
                it is this primitive's independent web-fetch evidence
                surface; use any stable placeholder URL if genuinely unused,
                but a real one enables the tracking-proof claim path).
            ship_by_ts: unix ts by which the seller must accept, else the
                buyer may reclaim.
            deliver_by_ts: unix ts by which delivery evidence is expected.
            now_ts: caller-supplied current unix time (this chain has no
                trusted mid-transaction clock read; validators re-derive
                deadlines from the same caller-supplied timestamps).

        Returns: the new deal id.
        """
        buyer = gl.message.sender_address
        seller_addr = seller if isinstance(seller, Address) else Address(seller)
        _require(not self._addr_eq(seller_addr, buyer), "buyer and seller must differ")

        price = int(gl.message.value)
        _require(price > 0, "attach the agreed price as value")
        _require(0 < len(item_title.strip()) <= MAX_TITLE_LEN, f"item_title must be 1..{MAX_TITLE_LEN} chars")
        _require(len(item_description) <= MAX_DESCRIPTION_LEN, "item_description too long")
        listing_url = _validate_url(listing_photo_url, "listing_photo_url")
        track_url = _validate_url(tracking_url, "tracking_url")
        _require(now_ts > 0, "now_ts must be a positive unix timestamp")
        _require(ship_by_ts > now_ts, "ship_by_ts must be in the future")
        _require(deliver_by_ts > ship_by_ts, "deliver_by_ts must be after ship_by_ts")

        deal_id = int(self.deal_count)
        self.deal_count = u64(deal_id + 1)
        did = u32(deal_id)

        self.deals[did] = Deal(
            id=did,
            buyer=buyer,
            seller=seller_addr,
            item_title=item_title.strip(),
            item_description=item_description.strip(),
            listing_photo_url=listing_url,
            tracking_url=track_url,
            delivery_photo_url="",
            status=u8(STATUS_CREATED),
            created_ts=u64(now_ts),
            ship_by_ts=u64(ship_by_ts),
            deliver_by_ts=u64(deliver_by_ts),
            price_wei=str(price),
            price_deposited_wei=str(price),
            seller_bond_wei="0",
            seller_bond_deposited_wei="0",
            contest_bond_wei="0",
            contest_bond_deposited_wei="0",
            contester="",
            tracking_status=u8(TRACK_UNKNOWN),
            tracking_checked=False,
            verdict_band=u8(BAND_NONE),
            seller_payout_bps=u32(0),
            verdict_reasoning="",
            final_band=u8(BAND_NONE),
            final_seller_payout_bps=u32(0),
            contest_outcome="",
            resolve_attempts=u32(0),
            contest_count=u32(0),
            resolved_ts=u64(0),
            finalized_ts=u64(0),
        )

        self._record_party(buyer, deal_id)
        self._record_party(seller_addr, deal_id)
        self.total_deals = u64(int(self.total_deals) + 1)
        self.total_volume_wei = u256(int(self.total_volume_wei) + price)
        self._log(deal_id, "CREATE", buyer, price, now_ts, item_title.strip()[:100])
        return deal_id

    @gl.public.write
    def cancel_deal(self, deal_id: int, now_ts: int) -> None:
        """Buyer cancels before the seller has accepted. Full refund."""
        deal = self._get_deal(deal_id)
        sender = gl.message.sender_address
        _require(self._addr_eq(deal.buyer, sender), "only the buyer may cancel")
        _require(int(deal.status) == STATUS_CREATED, "deal can only be cancelled before acceptance")

        refund = int(deal.price_deposited_wei)
        deal.price_deposited_wei = "0"
        deal.status = u8(STATUS_CANCELLED)
        deal.finalized_ts = u64(max(0, now_ts))
        self._log(deal_id, "CANCEL", sender, refund, now_ts, "")
        self._send_native(deal.buyer, refund)

    @gl.public.write.payable
    def accept_deal(self, deal_id: int, now_ts: int) -> None:
        """Seller accepts the deal, optionally posting a performance bond as
        attached value (0 is valid)."""
        deal = self._get_deal(deal_id)
        sender = gl.message.sender_address
        _require(self._addr_eq(deal.seller, sender), "only the named seller may accept")
        _require(int(deal.status) == STATUS_CREATED, "deal is not awaiting acceptance")
        _require(now_ts <= int(deal.ship_by_ts), "acceptance window has passed")

        bond = int(gl.message.value)
        deal.seller_bond_wei = str(bond)
        deal.seller_bond_deposited_wei = str(bond)
        deal.status = u8(STATUS_ACCEPTED)
        self._log(deal_id, "ACCEPT", sender, bond, now_ts, "")

    @gl.public.write
    def timeout_unaccepted_reclaim(self, deal_id: int, now_ts: int) -> None:
        """Permissionless: if the seller never accepted by ship_by_ts, the
        buyer's funds are recoverable by anyone calling this on their behalf."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_CREATED, "deal is not in an unaccepted state")
        _require(now_ts > int(deal.ship_by_ts), "acceptance window has not passed yet")

        refund = int(deal.price_deposited_wei)
        _require(refund > 0, "nothing to reclaim")
        deal.price_deposited_wei = "0"
        deal.status = u8(STATUS_TIMEOUT_UNACCEPTED)
        deal.finalized_ts = u64(max(0, now_ts))
        self._log(deal_id, "TIMEOUT_UNACCEPTED", gl.message.sender_address, refund, now_ts, "")
        self._send_native(deal.buyer, refund)

    @gl.public.write
    def submit_delivery_evidence(self, deal_id: int, delivery_photo_url: str, now_ts: int) -> None:
        """Buyer submits the arrival-condition photo once the item is in hand."""
        deal = self._get_deal(deal_id)
        sender = gl.message.sender_address
        _require(self._addr_eq(deal.buyer, sender), "only the buyer may submit delivery evidence")
        _require(int(deal.status) == STATUS_ACCEPTED, "deal is not awaiting delivery evidence")
        _require(
            now_ts <= int(deal.deliver_by_ts) + DELIVER_GRACE_SECONDS,
            "delivery evidence window has passed; use timeout_undelivered_reclaim",
        )
        url = _validate_url(delivery_photo_url, "delivery_photo_url")

        deal.delivery_photo_url = url
        deal.status = u8(STATUS_DELIVERY_SUBMITTED)
        self._log(deal_id, "DELIVERY_SUBMITTED", sender, 0, now_ts, "")

    @gl.public.write
    def timeout_undelivered_reclaim(self, deal_id: int, now_ts: int) -> None:
        """Permissionless: if the buyer never submitted delivery evidence and
        the outer grace window has elapsed, the buyer's funds (and the
        seller's bond, if any) are recoverable - the buyer is the party
        without the goods, so this timeout resolves in their favor."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_ACCEPTED, "deal already has delivery evidence or is resolved")
        _require(
            now_ts > int(deal.deliver_by_ts) + DELIVER_GRACE_SECONDS,
            "delivery grace window has not passed yet",
        )

        price = int(deal.price_deposited_wei)
        bond = int(deal.seller_bond_deposited_wei)
        deal.price_deposited_wei = "0"
        deal.seller_bond_deposited_wei = "0"
        deal.status = u8(STATUS_TIMEOUT_UNDELIVERED)
        deal.finalized_ts = u64(max(0, now_ts))
        self._log(deal_id, "TIMEOUT_UNDELIVERED", gl.message.sender_address, price + bond, now_ts, "")
        self._send_native(deal.buyer, price + bond)

    # ========================================================================
    #  PUBLIC WRITES - independent tracking-proof surface (web fetch)
    # ========================================================================

    @gl.public.write
    def check_delivery_status(self, deal_id: int, now_ts: int) -> dict:
        """Permissionless: fetch and classify the carrier tracking page.
        Does not move funds by itself - it records a fact (`tracking_status`)
        that (a) feeds as context into condition adjudication, and (b) can
        later support `claim_via_tracking_confirmation` if the buyer goes
        silent after real delivery. Deliberately a separate, cheaper round
        from the image adjudication so a slow carrier page never blocks it."""
        deal = self._get_deal(deal_id)
        _require(
            int(deal.status) in (STATUS_ACCEPTED, STATUS_DELIVERY_SUBMITTED),
            "deal is not in a state where tracking is relevant",
        )
        result = self._check_tracking_nondet(str(deal.tracking_url))
        deal.tracking_status = u8(result["status"])
        deal.tracking_checked = True
        self._log(
            deal_id,
            "TRACKING_CHECKED",
            gl.message.sender_address,
            0,
            now_ts,
            TRACK_NAMES[result["status"]],
        )
        return {"status": TRACK_NAMES[result["status"]], "reasoning": result["reasoning"]}

    @gl.public.write
    def claim_via_tracking_confirmation(self, deal_id: int, now_ts: int) -> None:
        """Seller-side recovery: if tracking has been checked and shows
        DELIVERED, and the buyer never submitted delivery evidence nor
        disputed within a shorter grace window, the seller may claim full
        payment on tracking proof alone. Requires `check_delivery_status` to
        have been called first (by anyone) - this never trusts an
        unverified claim of "it was delivered"."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_ACCEPTED, "deal already has delivery evidence or is resolved")
        _require(bool(deal.tracking_checked), "call check_delivery_status first")
        _require(int(deal.tracking_status) == TRACK_DELIVERED, "tracking does not show delivered")
        _require(
            now_ts > int(deal.deliver_by_ts) + SELLER_TRACKING_CLAIM_GRACE_SECONDS,
            "tracking-claim grace window has not passed yet",
        )
        _require(
            SELLER_TRACKING_CLAIM_GRACE_SECONDS < DELIVER_GRACE_SECONDS,
            "misconfigured grace windows",  # invariant, never actually trips
        )

        price = int(deal.price_deposited_wei)
        bond = int(deal.seller_bond_deposited_wei)
        deal.price_deposited_wei = "0"
        deal.seller_bond_deposited_wei = "0"
        deal.status = u8(STATUS_FINALIZED_TRACKING_CLAIM)
        deal.finalized_ts = u64(max(0, now_ts))
        self.total_finalized = u64(int(self.total_finalized) + 1)
        self._log(deal_id, "TRACKING_CLAIM", gl.message.sender_address, price + bond, now_ts, "")
        self._send_native(deal.seller, price + bond)

    # ========================================================================
    #  PUBLIC WRITES - condition adjudication and finalization
    # ========================================================================

    @gl.public.write
    def resolve_condition(self, deal_id: int, now_ts: int) -> dict:
        """Permissionless: run consensus image adjudication comparing the
        listing photo to the submitted delivery photo. Records a verdict but
        does NOT move funds yet - see `finalize_deal` / the contest ladder.
        Retryable from UNDETERMINED."""
        deal = self._get_deal(deal_id)
        status = int(deal.status)
        _require(
            status in (STATUS_DELIVERY_SUBMITTED, STATUS_UNDETERMINED),
            "deal is not awaiting condition adjudication",
        )
        _require(int(deal.resolve_attempts) < MAX_RESOLVE_ATTEMPTS, "adjudication attempts exhausted; use force_refund_undetermined")

        tracking_note = (
            f"tracking last classified as {TRACK_NAMES[int(deal.tracking_status)]}"
            if bool(deal.tracking_checked)
            else "tracking has not been checked"
        )

        try:
            verdict = self._adjudicate_condition_nondet(
                str(deal.listing_photo_url),
                str(deal.delivery_photo_url),
                str(deal.item_title),
                str(deal.item_description),
                tracking_note,
                adversarial=False,
            )
        except gl.vm.UserError as exc:
            msg = getattr(exc, "message", None) or str(exc)
            deal.resolve_attempts = u32(int(deal.resolve_attempts) + 1)
            deal.status = u8(STATUS_UNDETERMINED)
            self._log(deal_id, "UNDETERMINED", gl.message.sender_address, 0, now_ts, msg[:180])
            return {"status": "UNDETERMINED", "reason": msg}

        deal.verdict_band = u8(verdict["band"])
        deal.seller_payout_bps = u32(verdict["seller_payout_bps"])
        deal.verdict_reasoning = verdict["reasoning"]
        deal.resolve_attempts = u32(int(deal.resolve_attempts) + 1)
        deal.resolved_ts = u64(max(0, now_ts))
        deal.status = u8(STATUS_VERDICT_PENDING)
        self._log(
            deal_id,
            "VERDICT_RECORDED",
            gl.message.sender_address,
            0,
            now_ts,
            BAND_NAMES[verdict["band"]],
        )
        return {
            "status": "VERDICT_PENDING",
            "band": BAND_NAMES[verdict["band"]],
            "seller_payout_bps": verdict["seller_payout_bps"],
        }

    @gl.public.write
    def force_refund_undetermined(self, deal_id: int, now_ts: int) -> None:
        """Permissionless safety valve: if adjudication has repeatedly failed
        to converge and a further grace window has elapsed, refund the buyer
        in full rather than leaving funds stuck forever. Chosen as the safe
        failure direction because an undetermined visual judgment must never
        default to paying the seller for an unverified delivery."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_UNDETERMINED, "deal is not in an undetermined state")
        _require(int(deal.resolve_attempts) >= MAX_RESOLVE_ATTEMPTS, "adjudication attempts not yet exhausted")
        last_activity_ts = int(deal.resolved_ts) if int(deal.resolved_ts) > 0 else int(deal.deliver_by_ts)
        _require(
            now_ts > last_activity_ts + UNDETERMINED_GRACE_SECONDS,
            "undetermined grace window has not passed yet",
        )

        price = int(deal.price_deposited_wei)
        bond = int(deal.seller_bond_deposited_wei)
        deal.price_deposited_wei = "0"
        deal.seller_bond_deposited_wei = "0"
        deal.status = u8(STATUS_TIMEOUT_UNDETERMINED_REFUND)
        deal.finalized_ts = u64(max(0, now_ts))
        self._log(deal_id, "UNDETERMINED_REFUND", gl.message.sender_address, price + bond, now_ts, "")
        self._send_native(deal.buyer, price + bond)

    @gl.public.write
    def finalize_deal(self, deal_id: int, now_ts: int) -> None:
        """Permissionless: once the contest window has elapsed with no
        contest bonded, pay out per the recorded verdict."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_VERDICT_PENDING, "deal has no pending verdict to finalize")
        _require(
            now_ts > int(deal.resolved_ts) + CONTEST_WINDOW_SECONDS,
            "contest window has not passed yet",
        )

        band = int(deal.verdict_band)
        seller_bps = int(deal.seller_payout_bps)
        deal.final_band = u8(band)
        deal.final_seller_payout_bps = u32(seller_bps)
        deal.status = u8(self._terminal_status_for_band(band))
        deal.finalized_ts = u64(max(0, now_ts))
        self.total_finalized = u64(int(self.total_finalized) + 1)
        self._log(deal_id, "FINALIZED", gl.message.sender_address, 0, now_ts, BAND_NAMES[band])
        self._payout_for_band(deal, band, seller_bps)

    def _terminal_status_for_band(self, band: int) -> int:
        return {
            BAND_MATCH: STATUS_FINALIZED_MATCH,
            BAND_MINOR_DISCREPANCY: STATUS_FINALIZED_MINOR,
            BAND_MAJOR_DISCREPANCY: STATUS_FINALIZED_MAJOR,
            BAND_NOT_RECEIVED: STATUS_FINALIZED_NOT_RECEIVED,
        }[band]

    # ========================================================================
    #  PUBLIC WRITES - bonded single-round contest ladder
    # ========================================================================

    @gl.public.write.payable
    def contest_verdict(self, deal_id: int, now_ts: int) -> None:
        """Either party may bond CONTEST_BOND_BPS of the price, within the
        contest window, to force one additional adversarially-framed
        adjudication round before funds finalize. Capped at one contest per
        deal - a bounded escalation ladder, not an unbounded one."""
        deal = self._get_deal(deal_id)
        sender = gl.message.sender_address
        _require(
            self._addr_eq(deal.buyer, sender) or self._addr_eq(deal.seller, sender),
            "only the buyer or seller may contest",
        )
        _require(int(deal.status) == STATUS_VERDICT_PENDING, "no pending verdict to contest")
        _require(
            now_ts <= int(deal.resolved_ts) + CONTEST_WINDOW_SECONDS,
            "contest window has passed",
        )
        _require(int(deal.contest_count) == 0, "this deal has already used its one contest")

        required_bond = (int(deal.price_wei) * CONTEST_BOND_BPS) // BPS_DENOMINATOR
        posted = int(gl.message.value)
        _require(posted == required_bond, f"must post exactly the contest bond of {required_bond} wei")

        deal.contest_bond_wei = str(posted)
        deal.contest_bond_deposited_wei = str(posted)
        deal.contester = sender.as_hex
        deal.contest_count = u32(1)
        deal.status = u8(STATUS_CONTESTED)
        self.total_contests = u64(int(self.total_contests) + 1)
        self._log(deal_id, "CONTESTED", sender, posted, now_ts, "")

    @gl.public.write
    def resolve_contest(self, deal_id: int, now_ts: int) -> dict:
        """Permissionless: run the second, adversarially-framed adjudication
        and compare it to the originally recorded verdict. If upheld, the
        contester's bond is forfeited to the counterparty and the ORIGINAL
        verdict pays out. If overturned, the contester's bond is returned and
        the NEW verdict pays out instead."""
        deal = self._get_deal(deal_id)
        _require(int(deal.status) == STATUS_CONTESTED, "deal has no active contest")

        tracking_note = (
            f"tracking last classified as {TRACK_NAMES[int(deal.tracking_status)]}"
            if bool(deal.tracking_checked)
            else "tracking has not been checked"
        )

        try:
            new_verdict = self._adjudicate_condition_nondet(
                str(deal.listing_photo_url),
                str(deal.delivery_photo_url),
                str(deal.item_title),
                str(deal.item_description),
                tracking_note,
                adversarial=True,
            )
        except gl.vm.UserError:
            # A contest adjudication that itself fails to converge upholds
            # the original verdict rather than stalling the deal further -
            # the contester already accepted this risk by bonding.
            return self._settle_contest(deal, deal_id, now_ts, upheld=True, new_verdict=None)

        original_band = int(deal.verdict_band)
        original_bps = int(deal.seller_payout_bps)
        same_band = new_verdict["band"] == original_band
        bps_close = True
        if original_band == BAND_MINOR_DISCREPANCY and new_verdict["band"] == BAND_MINOR_DISCREPANCY:
            bps_close = abs(new_verdict["seller_payout_bps"] - original_bps) <= 1500
        upheld = same_band and bps_close
        return self._settle_contest(deal, deal_id, now_ts, upheld=upheld, new_verdict=new_verdict)

    def _settle_contest(self, deal: Deal, deal_id: int, now_ts: int, upheld: bool, new_verdict) -> dict:
        contest_bond = int(deal.contest_bond_deposited_wei)
        deal.contest_bond_deposited_wei = "0"
        contester_addr = Address(deal.contester)
        counterparty = deal.seller if self._addr_eq(deal.buyer, contester_addr) else deal.buyer

        if upheld:
            deal.contest_outcome = "UPHELD"
            final_band = int(deal.verdict_band)
            final_bps = int(deal.seller_payout_bps)
        else:
            deal.contest_outcome = "OVERTURNED"
            final_band = new_verdict["band"]
            final_bps = new_verdict["seller_payout_bps"]
            deal.verdict_band = u8(final_band)
            deal.seller_payout_bps = u32(final_bps)
            deal.verdict_reasoning = new_verdict["reasoning"]

        deal.final_band = u8(final_band)
        deal.final_seller_payout_bps = u32(final_bps)
        deal.status = u8(self._terminal_status_for_band(final_band))
        deal.finalized_ts = u64(max(0, now_ts))
        self.total_finalized = u64(int(self.total_finalized) + 1)

        self._log(
            deal_id,
            "CONTEST_RESOLVED",
            gl.message.sender_address,
            contest_bond,
            now_ts,
            deal.contest_outcome,
        )

        # Contest bond routes first (its own independent zero-then-transfer),
        # then the main escrow routes via the shared payout helper.
        if upheld:
            if contest_bond > 0:
                self._send_native(counterparty, contest_bond)
        else:
            if contest_bond > 0:
                self._send_native(contester_addr, contest_bond)

        self._payout_for_band(deal, final_band, final_bps)

        return {
            "outcome": deal.contest_outcome,
            "final_band": BAND_NAMES[final_band],
            "final_seller_payout_bps": final_bps,
        }

    # ========================================================================
    #  PUBLIC VIEWS
    # ========================================================================

    def _deal_dict(self, deal: Deal) -> dict:
        return {
            "id": int(deal.id),
            "buyer": deal.buyer.as_hex,
            "seller": deal.seller.as_hex,
            "item_title": deal.item_title,
            "item_description": deal.item_description,
            "listing_photo_url": deal.listing_photo_url,
            "tracking_url": deal.tracking_url,
            "delivery_photo_url": deal.delivery_photo_url,
            "status": STATUS_NAMES.get(int(deal.status), "UNKNOWN"),
            "created_ts": int(deal.created_ts),
            "ship_by_ts": int(deal.ship_by_ts),
            "deliver_by_ts": int(deal.deliver_by_ts),
            "price_wei": int(deal.price_wei),
            "price_deposited_wei": int(deal.price_deposited_wei),
            "seller_bond_wei": int(deal.seller_bond_wei),
            "seller_bond_deposited_wei": int(deal.seller_bond_deposited_wei),
            "contest_bond_wei": int(deal.contest_bond_wei),
            "contest_bond_deposited_wei": int(deal.contest_bond_deposited_wei),
            "contester": deal.contester,
            "tracking_status": TRACK_NAMES.get(int(deal.tracking_status), "UNKNOWN"),
            "tracking_checked": bool(deal.tracking_checked),
            "verdict_band": BAND_NAMES.get(int(deal.verdict_band), "NONE"),
            "seller_payout_bps": int(deal.seller_payout_bps),
            "verdict_reasoning": deal.verdict_reasoning,
            "final_band": BAND_NAMES.get(int(deal.final_band), "NONE"),
            "final_seller_payout_bps": int(deal.final_seller_payout_bps),
            "contest_outcome": deal.contest_outcome,
            "resolve_attempts": int(deal.resolve_attempts),
            "contest_count": int(deal.contest_count),
            "resolved_ts": int(deal.resolved_ts),
            "finalized_ts": int(deal.finalized_ts),
        }

    @gl.public.view
    def get_deal(self, deal_id: int) -> dict:
        return self._deal_dict(self._get_deal(deal_id))

    @gl.public.view
    def get_deal_summary(self, deal_id: int) -> dict:
        deal = self._get_deal(deal_id)
        return {
            "id": int(deal.id),
            "status": STATUS_NAMES.get(int(deal.status), "UNKNOWN"),
            "price_wei": int(deal.price_wei),
            "final_band": BAND_NAMES.get(int(deal.final_band), "NONE"),
        }

    @gl.public.view
    def get_deal_count(self) -> int:
        return int(self.deal_count)

    @gl.public.view
    def get_party_deal_ids(self, address: str) -> list:
        arr = self.party_deal_ids.get(Address(address))
        if arr is None:
            return []
        return [int(x) for x in arr]

    @gl.public.view
    def get_activity(self, deal_id: int, offset: int = 0, limit: int = 25) -> list:
        self._get_deal(deal_id)
        log = self.activity.get(u32(deal_id))
        if log is None:
            return []
        total = len(log)
        capped = _clamp_int(int(limit), 1, 100)
        start = total - 1 - max(0, int(offset))
        result = []
        idx = start
        while idx >= 0 and len(result) < capped:
            evt = log[idx]
            result.append(
                {
                    "kind": evt.kind,
                    "actor": evt.actor.as_hex,
                    "amount": int(evt.amount),
                    "ts": int(evt.ts),
                    "note": evt.note,
                }
            )
            idx -= 1
        return result

    @gl.public.view
    def get_platform_stats(self) -> dict:
        return {
            "total_deals": int(self.total_deals),
            "total_volume_wei": int(self.total_volume_wei),
            "total_finalized": int(self.total_finalized),
            "total_contests": int(self.total_contests),
        }

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "contest_bond_bps": CONTEST_BOND_BPS,
            "contest_window_seconds": CONTEST_WINDOW_SECONDS,
            "seller_tracking_claim_grace_seconds": SELLER_TRACKING_CLAIM_GRACE_SECONDS,
            "deliver_grace_seconds": DELIVER_GRACE_SECONDS,
            "undetermined_grace_seconds": UNDETERMINED_GRACE_SECONDS,
            "max_resolve_attempts": MAX_RESOLVE_ATTEMPTS,
        }
