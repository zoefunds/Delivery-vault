"""Full-surface StudioNet integration test: drives every write method and
reads every view against the live deployed DeliveryVault, with a real buyer
and seller account, real GEN escrow, and real web/LLM consensus rounds.

Time is no longer a caller-supplied argument anywhere in this contract (see
the `_now_ts` fix in `contracts/delivery_vault.py`), which means it is also
no longer fakeable here: `finalize_deal` and `force_refund_undetermined`
genuinely require the real CONTEST_WINDOW_SECONDS / grace window to elapse
on the live network before they can succeed. This test drives every
call that does NOT require waiting out a real multi-hour/day window inline,
and documents (rather than fakes) the ones that do.

Run: pytest tests/integration/test_live_lifecycle.py -v -s
"""

import json
import time
from pathlib import Path

import pytest
from eth_account import Account
from gltest.contracts import get_contract_factory
from gltest.assertions import tx_execution_failed

CONTRACT_ADDRESS = "0x43d4a534E9761D2CC359b2D6e5af1d6D6Bf8602d"
KEYS_DIR = Path(__file__).parent.parent.parent / ".keys"
GEN = 10**18
PASSWORD = "testpass123"


def _load_account(name: str):
    with open(KEYS_DIR / f"{name}.json") as f:
        encrypted = json.load(f)
    private_key = Account.decrypt(encrypted, PASSWORD)
    return Account.from_key(private_key)


@pytest.mark.integration
def test_full_lifecycle_up_to_verdict_on_studionet():
    """Drives create_deal -> accept_deal -> submit_delivery_evidence ->
    check_delivery_status -> resolve_condition live. Stops at
    VERDICT_PENDING/UNDETERMINED rather than calling finalize_deal, because
    finalize_deal now genuinely requires CONTEST_WINDOW_SECONDS (48h) of
    real elapsed time to pass -- there is no caller-supplied timestamp left
    to fake it with, which is the entire point of the fix."""
    buyer = _load_account("buyer")
    seller = _load_account("seller")

    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)
    c_seller = factory.build_contract(CONTRACT_ADDRESS, account=seller)
    c_anyone = c_buyer  # permissionless calls, reuse the buyer's signer

    now = int(time.time())
    ship_by = now + 600
    deliver_by = ship_by + 600

    count_before = c_anyone.get_deal_count().call()

    receipt = c_buyer.create_deal(
        args=[
            seller.address,
            "Mid-century modern sofa",
            "Grey linen three-seater, light use, no stains or tears, all cushions included",
            "https://httpbin.org/image/jpeg",
            "https://httpbin.org/status/200",
            ship_by,
            deliver_by,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("\ncreate_deal:", receipt.get("status_name"))

    deal_id = c_anyone.get_deal_count().call() - 1
    assert deal_id == count_before
    print("deal_id:", deal_id)

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["status"] == "CREATED"
    assert deal["price_deposited_wei"] == 1 * GEN
    print("post-create state:", json.dumps(deal, indent=2))

    receipt = c_seller.accept_deal(args=[deal_id]).transact(
        value=int(0.1 * GEN), wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("accept_deal:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["status"] == "ACCEPTED"
    assert deal["seller_bond_deposited_wei"] == int(0.1 * GEN)

    receipt = c_buyer.submit_delivery_evidence(
        args=[deal_id, "https://httpbin.org/image/jpeg"]
    ).transact(wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("submit_delivery_evidence:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["status"] == "DELIVERY_SUBMITTED"

    receipt = c_anyone.check_delivery_status(args=[deal_id]).transact(
        wait_interval=8000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("check_delivery_status:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["tracking_checked"] is True
    print("tracking_status:", deal["tracking_status"])

    receipt = c_anyone.resolve_condition(args=[deal_id]).transact(
        wait_interval=8000, wait_retries=120
    )
    assert not tx_execution_failed(receipt), receipt
    print("resolve_condition:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    print("post-adjudication state:", json.dumps(deal, indent=2))
    assert deal["status"] in ("VERDICT_PENDING", "UNDETERMINED")
    assert deal["price_deposited_wei"] == 1 * GEN  # untouched pre-finalization

    if deal["status"] == "VERDICT_PENDING":
        # contest_verdict has no time gate other than "within the still-open
        # window", which trivially holds seconds after resolve_condition.
        receipt = c_seller.contest_verdict(args=[deal_id]).transact(
            value=int(0.15 * GEN), wait_interval=5000, wait_retries=90
        )
        assert not tx_execution_failed(receipt), receipt
        print("contest_verdict:", receipt.get("status_name"))

        receipt = c_anyone.resolve_contest(args=[deal_id]).transact(
            wait_interval=8000, wait_retries=120
        )
        assert not tx_execution_failed(receipt), receipt
        print("resolve_contest:", receipt.get("status_name"))

        final_deal = c_anyone.get_deal(args=[deal_id]).call()
        print("FINAL state (via contest, no wait required):", json.dumps(final_deal, indent=2))
        assert final_deal["status"].startswith("FINALIZED")
        assert final_deal["price_deposited_wei"] == 0
        assert final_deal["seller_bond_deposited_wei"] == 0

    stats = c_anyone.get_platform_stats().call()
    print("platform stats:", json.dumps(stats, indent=2))

    activity = c_anyone.get_activity(args=[deal_id]).call()
    kinds = [a["kind"] for a in activity]
    print("activity kinds:", kinds)
    assert "CREATE" in kinds
    assert "ACCEPT" in kinds
    assert "DELIVERY_SUBMITTED" in kinds


@pytest.mark.integration
def test_convergence_condition_adjudication_is_deterministic_given_same_evidence():
    """Convergence property: two independent calls to resolve_condition for
    two separate deals funded with IDENTICAL listing/delivery photos and
    description must reach the SAME verdict band -- the strict form of the
    property this primitive depends on validators agreeing about."""
    buyer = _load_account("buyer")
    seller = _load_account("seller")
    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)
    c_seller = factory.build_contract(CONTRACT_ADDRESS, account=seller)

    listing_url = "https://httpbin.org/image/jpeg"
    delivery_url = "https://httpbin.org/image/jpeg"  # identical photo

    bands = []
    for i in range(2):
        now = int(time.time())
        ship_by = now + 600
        deliver_by = ship_by + 600
        receipt = c_buyer.create_deal(
            args=[
                seller.address,
                "Convergence check item",
                "Identical listing and delivery photo for a strict convergence test",
                listing_url,
                "https://httpbin.org/status/200",
                ship_by,
                deliver_by,
            ]
        ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
        assert not tx_execution_failed(receipt), receipt
        deal_id = c_buyer.get_deal_count().call() - 1

        c_seller.accept_deal(args=[deal_id]).transact(
            wait_interval=5000, wait_retries=90
        )
        c_buyer.submit_delivery_evidence(args=[deal_id, delivery_url]).transact(
            wait_interval=5000, wait_retries=90
        )
        receipt = c_buyer.resolve_condition(args=[deal_id]).transact(
            wait_interval=8000, wait_retries=120
        )
        assert not tx_execution_failed(receipt), receipt
        deal = c_buyer.get_deal(args=[deal_id]).call()
        print(f"run {i}: band={deal['verdict_band']} status={deal['status']}")
        bands.append(deal["verdict_band"])

    assert bands[0] == bands[1], f"convergence failed: {bands}"
    assert bands[0] == "MATCH", f"identical photos should converge on MATCH, got {bands}"


@pytest.mark.integration
def test_cancel_deal_live():
    """Exercises cancel_deal live: buyer funds, then cancels before the
    seller accepts, and the full price is refunded."""
    buyer = _load_account("buyer")
    seller = _load_account("seller")
    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)

    now = int(time.time())
    ship_by = now + 600
    deliver_by = ship_by + 600

    receipt = c_buyer.create_deal(
        args=[
            seller.address,
            "Cancel-path coverage item",
            "Exercises cancel_deal live",
            "https://httpbin.org/image/jpeg",
            "https://httpbin.org/status/200",
            ship_by,
            deliver_by,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1
    print("cancel_deal test deal_id:", deal_id)

    receipt = c_buyer.cancel_deal(args=[deal_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("cancel_deal:", receipt.get("status_name"))

    deal = c_buyer.get_deal(args=[deal_id]).call()
    assert deal["status"] == "CANCELLED"
    assert deal["price_deposited_wei"] == 0


@pytest.mark.integration
def test_timeout_unaccepted_reclaim_live():
    """Exercises timeout_unaccepted_reclaim live: buyer funds with a very
    short ship_by_ts, waits for it to genuinely elapse (real time, not
    faked), then reclaims permissionlessly."""
    buyer = _load_account("buyer")
    seller = _load_account("seller")
    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)

    now = int(time.time())
    ship_by = now + 20  # short window; we genuinely wait it out below
    deliver_by = ship_by + 600

    receipt = c_buyer.create_deal(
        args=[
            seller.address,
            "Timeout-path coverage item",
            "Exercises timeout_unaccepted_reclaim live",
            "https://httpbin.org/image/jpeg",
            "https://httpbin.org/status/200",
            ship_by,
            deliver_by,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1
    print("timeout test deal_id:", deal_id)

    # Genuinely wait for ship_by_ts to pass -- there is no way to fake this
    # anymore, which is the entire point of the fix.
    while int(time.time()) <= ship_by:
        time.sleep(2)

    receipt = c_buyer.timeout_unaccepted_reclaim(args=[deal_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("timeout_unaccepted_reclaim:", receipt.get("status_name"))

    deal = c_buyer.get_deal(args=[deal_id]).call()
    assert deal["status"] == "TIMEOUT_UNACCEPTED"
    assert deal["price_deposited_wei"] == 0
