"""Direct (in-memory) unit tests for the Physical-Goods Delivery-Condition
Vault. Runs the contract natively via gltest.direct — no simulator, no
network. Web and LLM calls are mocked; live consensus behavior is covered
separately by the StudioNet integration evidence in docs/.

Run: pytest tests/direct/ -v
"""

import json
import time
from pathlib import Path

import pytest
from gltest.direct import VMContext, deploy_contract, create_address

CONTRACT = Path(__file__).parent.parent.parent / "contracts" / "delivery_vault.py"

GEN = 10**18
NOW = int(time.time())
SHIP_BY = NOW + 3 * 86400
DELIVER_BY = SHIP_BY + 7 * 86400

LISTING_URL = "https://cdn.example.com/listing/couch.jpg"
TRACKING_URL = "https://tracking.example.com/abc123"
DELIVERY_URL = "https://cdn.example.com/delivery/couch-arrived.jpg"

FAKE_IMAGE = b"\xff\xd8\xff\xe0FAKEJPEGBYTES" * 4  # nonempty bytes; mock layer never decodes it


def hx(addr) -> str:
    if isinstance(addr, bytes):
        return "0x" + addr.hex()
    return addr.as_hex


BUYER = create_address("buyer")
SELLER = create_address("seller")
OTHER = create_address("other")


def fresh(vm: VMContext):
    vm.sender = BUYER
    vm.value = 0
    return deploy_contract(CONTRACT, vm)


def mock_photos(vm, listing_status=200, delivery_status=200, listing_body=FAKE_IMAGE, delivery_body=FAKE_IMAGE):
    vm.mock_web(r".*listing.*", {"status": listing_status, "body": listing_body})
    vm.mock_web(r".*delivery.*", {"status": delivery_status, "body": delivery_body})


def mock_condition_verdict(vm, band, seller_payout_bps=0, reasoning="matches"):
    vm.mock_llm(
        r".*",
        json.dumps({"band": band, "seller_payout_bps": seller_payout_bps, "reasoning": reasoning}),
    )


def mock_tracking(vm, status="DELIVERED"):
    vm.mock_web(r".*tracking.*", {"status": 200, "body": "Package status: delivered to front porch."})
    vm.mock_llm(r".*", json.dumps({"status": status, "reasoning": "page said so"}))


def make_deal(vm, c, ship_by=SHIP_BY, deliver_by=DELIVER_BY, price=1 * GEN) -> int:
    vm.sender = BUYER
    vm.value = price
    deal_id = c.create_deal(
        hx(SELLER),
        "Mid-century sofa",
        "Grey fabric three-seater, no visible damage",
        LISTING_URL,
        TRACKING_URL,
        ship_by,
        deliver_by,
        NOW,
    )
    vm.value = 0
    return deal_id


def accept(vm, c, deal_id, bond=0, now_ts=None):
    vm.sender = SELLER
    vm.value = bond
    c.accept_deal(deal_id, now_ts if now_ts is not None else NOW + 1000)
    vm.value = 0


def submit_evidence(vm, c, deal_id, now_ts=None):
    vm.sender = BUYER
    vm.value = 0
    c.submit_delivery_evidence(deal_id, DELIVERY_URL, now_ts if now_ts is not None else DELIVER_BY - 1000)


# ---------------------------------------------------------------------------
# Deployment & deal creation
# ---------------------------------------------------------------------------

def test_deploy_fresh_state():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        assert c.get_deal_count() == 0
        cfg = c.get_config()
        assert cfg["contest_bond_bps"] == 1500
        assert cfg["max_resolve_attempts"] == 5


def test_create_deal_happy_path_records_escrow():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        assert did == 0
        deal = c.get_deal(0)
        assert deal["status"] == "CREATED"
        assert deal["price_wei"] == 1 * GEN
        assert deal["price_deposited_wei"] == 1 * GEN
        assert hx(BUYER).lower() == deal["buyer"].lower()
        assert hx(SELLER).lower() == deal["seller"].lower()
        assert c.get_party_deal_ids(hx(BUYER)) == [0]
        assert c.get_party_deal_ids(hx(SELLER)) == [0]


def test_create_deal_requires_positive_value():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 0
        with vm.expect_revert():
            c.create_deal(hx(SELLER), "x", "d", LISTING_URL, TRACKING_URL, SHIP_BY, DELIVER_BY, NOW)


def test_create_deal_rejects_same_buyer_and_seller():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 1 * GEN
        with vm.expect_revert():
            c.create_deal(hx(BUYER), "x", "d", LISTING_URL, TRACKING_URL, SHIP_BY, DELIVER_BY, NOW)


def test_create_deal_rejects_bad_photo_url():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 1 * GEN
        with vm.expect_revert():
            c.create_deal(hx(SELLER), "x", "d", "ftp://not-http", TRACKING_URL, SHIP_BY, DELIVER_BY, NOW)


def test_create_deal_rejects_deliver_before_ship():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 1 * GEN
        with vm.expect_revert():
            c.create_deal(hx(SELLER), "x", "d", LISTING_URL, TRACKING_URL, SHIP_BY, SHIP_BY - 10, NOW)


def test_create_deal_rejects_ship_by_in_the_past():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 1 * GEN
        with vm.expect_revert():
            c.create_deal(hx(SELLER), "x", "d", LISTING_URL, TRACKING_URL, NOW - 10, DELIVER_BY, NOW)


# ---------------------------------------------------------------------------
# Cancellation (pre-acceptance refund path)
# ---------------------------------------------------------------------------

def test_cancel_deal_refunds_buyer_pre_acceptance():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        vm.sender = BUYER
        c.cancel_deal(did, NOW + 10)
        deal = c.get_deal(did)
        assert deal["status"] == "CANCELLED"
        assert deal["price_deposited_wei"] == 0


def test_cancel_deal_only_buyer_may_cancel():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        vm.sender = SELLER
        with vm.expect_revert():
            c.cancel_deal(did, NOW + 10)


def test_cancel_deal_rejected_after_acceptance():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        vm.sender = BUYER
        with vm.expect_revert():
            c.cancel_deal(did, NOW + 2000)


# ---------------------------------------------------------------------------
# Acceptance and the unaccepted-timeout ladder
# ---------------------------------------------------------------------------

def test_accept_deal_only_named_seller():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        vm.sender = OTHER
        vm.value = 0
        with vm.expect_revert():
            c.accept_deal(did, NOW + 10)


def test_accept_deal_with_bond_recorded():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.1 * GEN))
        deal = c.get_deal(did)
        assert deal["status"] == "ACCEPTED"
        assert deal["seller_bond_deposited_wei"] == int(0.1 * GEN)


def test_accept_deal_after_ship_by_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        vm.sender = SELLER
        vm.value = 0
        with vm.expect_revert():
            c.accept_deal(did, SHIP_BY + 1)


def test_timeout_unaccepted_reclaim_before_deadline_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        with vm.expect_revert():
            c.timeout_unaccepted_reclaim(did, SHIP_BY - 1)


def test_timeout_unaccepted_reclaim_refunds_after_deadline():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        c.timeout_unaccepted_reclaim(did, SHIP_BY + 1)
        deal = c.get_deal(did)
        assert deal["status"] == "TIMEOUT_UNACCEPTED"
        assert deal["price_deposited_wei"] == 0


def test_timeout_unaccepted_reclaim_twice_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        c.timeout_unaccepted_reclaim(did, SHIP_BY + 1)
        with vm.expect_revert():
            c.timeout_unaccepted_reclaim(did, SHIP_BY + 2)


# ---------------------------------------------------------------------------
# Delivery evidence submission and the undelivered-timeout ladder
# ---------------------------------------------------------------------------

def test_submit_delivery_evidence_only_buyer():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        vm.sender = SELLER
        with vm.expect_revert():
            c.submit_delivery_evidence(did, DELIVERY_URL, DELIVER_BY - 100)


def test_submit_delivery_evidence_requires_accepted_state():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        vm.sender = BUYER
        with vm.expect_revert():
            c.submit_delivery_evidence(did, DELIVERY_URL, NOW + 100)  # never accepted


def test_submit_delivery_evidence_after_outer_grace_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        vm.sender = BUYER
        with vm.expect_revert():
            c.submit_delivery_evidence(did, DELIVERY_URL, DELIVER_BY + 7 * 86400 + 10)


def test_timeout_undelivered_reclaim_refunds_buyer_and_bond():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.05 * GEN))
        c.timeout_undelivered_reclaim(did, DELIVER_BY + 7 * 86400 + 1)
        deal = c.get_deal(did)
        assert deal["status"] == "TIMEOUT_UNDELIVERED"
        assert deal["price_deposited_wei"] == 0
        assert deal["seller_bond_deposited_wei"] == 0


def test_timeout_undelivered_reclaim_before_grace_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        with vm.expect_revert():
            c.timeout_undelivered_reclaim(did, DELIVER_BY + 1)


def test_timeout_undelivered_reclaim_blocked_once_evidence_submitted():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        with vm.expect_revert():
            c.timeout_undelivered_reclaim(did, DELIVER_BY + 7 * 86400 + 1)


# ---------------------------------------------------------------------------
# Independent web-fetch tracking surface
# ---------------------------------------------------------------------------

def test_check_delivery_status_records_tracking():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        mock_tracking(vm, "DELIVERED")
        result = c.check_delivery_status(did, DELIVER_BY - 100)
        assert result["status"] == "DELIVERED"
        deal = c.get_deal(did)
        assert deal["tracking_checked"] is True
        assert deal["tracking_status"] == "DELIVERED"


def test_claim_via_tracking_confirmation_requires_checked_first():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        with vm.expect_revert():
            c.claim_via_tracking_confirmation(did, DELIVER_BY + 3 * 86400 + 1)


def test_claim_via_tracking_confirmation_requires_delivered_status():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        mock_tracking(vm, "IN_TRANSIT")
        c.check_delivery_status(did, DELIVER_BY - 100)
        with vm.expect_revert():
            c.claim_via_tracking_confirmation(did, DELIVER_BY + 3 * 86400 + 1)


def test_claim_via_tracking_confirmation_before_grace_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        mock_tracking(vm, "DELIVERED")
        c.check_delivery_status(did, DELIVER_BY - 100)
        with vm.expect_revert():
            c.claim_via_tracking_confirmation(did, DELIVER_BY + 1)


def test_claim_via_tracking_confirmation_pays_seller_after_grace():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.1 * GEN))
        mock_tracking(vm, "DELIVERED")
        c.check_delivery_status(did, DELIVER_BY - 100)
        c.claim_via_tracking_confirmation(did, DELIVER_BY + 3 * 86400 + 1)
        deal = c.get_deal(did)
        assert deal["status"] == "FINALIZED_TRACKING_CLAIM"
        assert deal["price_deposited_wei"] == 0
        assert deal["seller_bond_deposited_wei"] == 0


def test_claim_via_tracking_confirmation_blocked_once_evidence_submitted():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        mock_tracking(vm, "DELIVERED")
        c.check_delivery_status(did, DELIVER_BY - 100)
        submit_evidence(vm, c, did)
        with vm.expect_revert():
            c.claim_via_tracking_confirmation(did, DELIVER_BY + 3 * 86400 + 1)


# ---------------------------------------------------------------------------
# Condition adjudication — image evidence
# ---------------------------------------------------------------------------

def test_resolve_condition_match_records_verdict_pending():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["status"] == "VERDICT_PENDING"
        assert result["band"] == "MATCH"
        deal = c.get_deal(did)
        assert deal["status"] == "VERDICT_PENDING"
        assert deal["price_deposited_wei"] == 1 * GEN  # not yet paid out


def test_resolve_condition_not_received_when_delivery_photo_unfetchable():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm, delivery_status=404, delivery_body="")
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["band"] == "NOT_RECEIVED"


def test_resolve_condition_minor_discrepancy_bps_banded():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MINOR_DISCREPANCY", seller_payout_bps=7234)  # should bucket to 7000 or 7500
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["band"] == "MINOR_DISCREPANCY"
        assert result["seller_payout_bps"] % 500 == 0


def test_resolve_condition_major_discrepancy():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MAJOR_DISCREPANCY")
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["band"] == "MAJOR_DISCREPANCY"


def test_resolve_condition_malformed_llm_output_sets_undetermined():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        vm.mock_llm(r".*", "not even json")
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["status"] == "UNDETERMINED"
        deal = c.get_deal(did)
        assert deal["status"] == "UNDETERMINED"
        assert deal["resolve_attempts"] == 1
        assert deal["price_deposited_wei"] == 1 * GEN  # funds untouched


def test_resolve_condition_unrecognized_band_name_sets_undetermined():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        vm.mock_llm(r".*", json.dumps({"band": "PERFECT", "reasoning": "great"}))
        result = c.resolve_condition(did, DELIVER_BY)
        assert result["status"] == "UNDETERMINED"


def test_resolve_condition_retries_after_undetermined():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        vm.mock_llm(r".*", "garbage")
        c.resolve_condition(did, DELIVER_BY)
        vm.clear_mocks()
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")
        result = c.resolve_condition(did, DELIVER_BY + 10)
        assert result["status"] == "VERDICT_PENDING"
        assert c.get_deal(did)["resolve_attempts"] == 2


def test_force_refund_undetermined_requires_max_attempts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        vm.mock_llm(r".*", "garbage")
        c.resolve_condition(did, DELIVER_BY)
        with vm.expect_revert():
            c.force_refund_undetermined(did, DELIVER_BY + 999999)


def test_force_refund_undetermined_refunds_buyer_after_exhaustion():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.05 * GEN))
        submit_evidence(vm, c, did)
        mock_photos(vm)
        vm.mock_llm(r".*", "garbage")
        t = DELIVER_BY
        for _ in range(5):
            c.resolve_condition(did, t)
            t += 10
        with vm.expect_revert():  # attempts exhausted but grace not elapsed
            c.force_refund_undetermined(did, t + 1)
        c.force_refund_undetermined(did, t + 3 * 86400 + 100)
        deal = c.get_deal(did)
        assert deal["status"] == "TIMEOUT_UNDETERMINED_REFUND"
        assert deal["price_deposited_wei"] == 0
        assert deal["seller_bond_deposited_wei"] == 0


def test_resolve_condition_requires_delivery_submitted_state():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        with vm.expect_revert():
            c.resolve_condition(did, NOW + 100)  # no delivery evidence yet


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------

def test_finalize_deal_before_contest_window_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")
        c.resolve_condition(did, DELIVER_BY)
        with vm.expect_revert():
            c.finalize_deal(did, DELIVER_BY + 1)


def test_finalize_deal_pays_match_in_full_to_seller():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.1 * GEN))
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")
        c.resolve_condition(did, DELIVER_BY)
        c.finalize_deal(did, DELIVER_BY + 172800 + 1)
        deal = c.get_deal(did)
        assert deal["status"] == "FINALIZED_MATCH"
        assert deal["price_deposited_wei"] == 0
        assert deal["seller_bond_deposited_wei"] == 0


def test_finalize_deal_pays_major_discrepancy_refunds_buyer():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did, bond=int(0.1 * GEN))
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MAJOR_DISCREPANCY")
        c.resolve_condition(did, DELIVER_BY)
        c.finalize_deal(did, DELIVER_BY + 172800 + 1)
        deal = c.get_deal(did)
        assert deal["status"] == "FINALIZED_MAJOR_DISCREPANCY"
        assert deal["price_deposited_wei"] == 0
        assert deal["seller_bond_deposited_wei"] == 0


def test_finalize_deal_twice_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        submit_evidence(vm, c, did)
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")
        c.resolve_condition(did, DELIVER_BY)
        c.finalize_deal(did, DELIVER_BY + 172800 + 1)
        with vm.expect_revert():
            c.finalize_deal(did, DELIVER_BY + 172800 + 2)


# ---------------------------------------------------------------------------
# Bonded contest ladder
# ---------------------------------------------------------------------------

def _resolve_to_pending(vm, c, did, band="MAJOR_DISCREPANCY", bps=0):
    accept(vm, c, did, bond=int(0.1 * GEN))
    submit_evidence(vm, c, did)
    mock_photos(vm)
    mock_condition_verdict(vm, band, seller_payout_bps=bps)
    c.resolve_condition(did, DELIVER_BY)


def test_contest_verdict_requires_exact_bond():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did)
        vm.sender = SELLER
        vm.value = int(0.1 * GEN)  # wrong amount (bond is 15% of price)
        with vm.expect_revert():
            c.contest_verdict(did, DELIVER_BY + 10)
        vm.value = 0


def test_contest_verdict_only_buyer_or_seller():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did)
        vm.sender = OTHER
        vm.value = int(0.15 * GEN)
        with vm.expect_revert():
            c.contest_verdict(did, DELIVER_BY + 10)
        vm.value = 0


def test_contest_verdict_after_window_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did)
        vm.sender = SELLER
        vm.value = int(0.15 * GEN)
        with vm.expect_revert():
            c.contest_verdict(did, DELIVER_BY + 172800 + 1)
        vm.value = 0


def test_contest_verdict_only_once_per_deal():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did)
        vm.sender = SELLER
        vm.value = int(0.15 * GEN)
        c.contest_verdict(did, DELIVER_BY + 10)
        vm.value = 0

        # Resolve the contest so status leaves CONTESTED, then a hypothetical
        # second contest attempt on the same (now finalized) deal must fail
        # because the deal is no longer VERDICT_PENDING/CONTESTED.
        mock_photos(vm)
        mock_condition_verdict(vm, "MAJOR_DISCREPANCY")
        c.resolve_contest(did, DELIVER_BY + 20)
        vm.sender = BUYER
        vm.value = int(0.15 * GEN)
        with vm.expect_revert():
            c.contest_verdict(did, DELIVER_BY + 30)
        vm.value = 0


def test_resolve_contest_upheld_forfeits_bond_to_counterparty():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did, band="MAJOR_DISCREPANCY")
        vm.sender = SELLER  # seller contests the MAJOR_DISCREPANCY verdict
        vm.value = int(0.15 * GEN)
        c.contest_verdict(did, DELIVER_BY + 10)
        vm.value = 0

        mock_photos(vm)
        mock_condition_verdict(vm, "MAJOR_DISCREPANCY")  # second opinion agrees
        result = c.resolve_contest(did, DELIVER_BY + 20)
        assert result["outcome"] == "UPHELD"
        deal = c.get_deal(did)
        assert deal["contest_outcome"] == "UPHELD"
        assert deal["status"] == "FINALIZED_MAJOR_DISCREPANCY"
        assert deal["contest_bond_deposited_wei"] == 0
        assert deal["price_deposited_wei"] == 0


def test_resolve_contest_overturned_reroutes_payout():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did, band="MAJOR_DISCREPANCY")
        vm.sender = SELLER
        vm.value = int(0.15 * GEN)
        c.contest_verdict(did, DELIVER_BY + 10)
        vm.value = 0

        vm.clear_mocks()
        mock_photos(vm)
        mock_condition_verdict(vm, "MATCH")  # second opinion overturns to MATCH
        result = c.resolve_contest(did, DELIVER_BY + 20)
        assert result["outcome"] == "OVERTURNED"
        deal = c.get_deal(did)
        assert deal["contest_outcome"] == "OVERTURNED"
        assert deal["status"] == "FINALIZED_MATCH"
        assert deal["final_band"] == "MATCH"


def test_resolve_contest_requires_contested_state():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        _resolve_to_pending(vm, c, did)
        with vm.expect_revert():
            c.resolve_contest(did, DELIVER_BY + 10)  # never contested


# ---------------------------------------------------------------------------
# Views / activity
# ---------------------------------------------------------------------------

def test_get_activity_and_platform_stats():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        accept(vm, c, did)
        activity = c.get_activity(did)
        kinds = [a["kind"] for a in activity]
        assert "CREATE" in kinds and "ACCEPT" in kinds
        stats = c.get_platform_stats()
        assert stats["total_deals"] == 1
        assert stats["total_volume_wei"] == 1 * GEN


def test_get_deal_summary_matches_full_deal():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        did = make_deal(vm, c)
        summary = c.get_deal_summary(did)
        full = c.get_deal(did)
        assert summary["status"] == full["status"]
        assert summary["price_wei"] == full["price_wei"]


def test_get_deal_unknown_id_reverts():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        with vm.expect_revert():
            c.get_deal(999)
