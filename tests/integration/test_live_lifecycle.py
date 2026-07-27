"""Full-surface StudioNet integration test: drives every write method and
reads every view against the live deployed DeliveryVault, with a real buyer
and seller account, real GEN escrow, and real web/LLM consensus rounds.

Run: pytest tests/integration/test_live_lifecycle.py -v -s
"""

import json
import time
from pathlib import Path

import pytest
from eth_account import Account
from gltest.contracts import get_contract_factory
from gltest.assertions import tx_execution_failed

CONTRACT_ADDRESS = "0xd661bea0F9796CA39d8bA4BBe5cF09E7C7138758"
KEYS_DIR = Path(__file__).parent.parent.parent / ".keys"
GEN = 10**18
PASSWORD = "testpass123"


def _load_account(name: str):
    with open(KEYS_DIR / f"{name}.json") as f:
        encrypted = json.load(f)
    private_key = Account.decrypt(encrypted, PASSWORD)
    return Account.from_key(private_key)


@pytest.mark.integration
def test_full_lifecycle_on_studionet():
    buyer = _load_account("buyer")
    seller = _load_account("seller")

    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)
    c_seller = factory.build_contract(CONTRACT_ADDRESS, account=seller)
    c_anyone = c_buyer  # permissionless calls, reuse the buyer's signer

    now = int(time.time())
    ship_by = now + 120
    deliver_by = ship_by + 120

    count_before = c_anyone.get_deal_count().call()

    receipt = c_buyer.create_deal(
        args=[
            seller.address,
            "Mid-century modern sofa",
            "Grey linen three-seater, light use, no stains or tears, all cushions included",
            "https://picsum.photos/seed/dvault-listing3/800/600.jpg",
            "https://httpbin.org/status/200",
            ship_by,
            deliver_by,
            now,
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

    receipt = c_seller.accept_deal(args=[deal_id, now + 5]).transact(
        value=int(0.1 * GEN), wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("accept_deal:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["status"] == "ACCEPTED"
    assert deal["seller_bond_deposited_wei"] == int(0.1 * GEN)

    receipt = c_buyer.submit_delivery_evidence(
        args=[deal_id, "https://picsum.photos/seed/dvault-arrived3/800/600.jpg", now + 10]
    ).transact(wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("submit_delivery_evidence:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["status"] == "DELIVERY_SUBMITTED"

    receipt = c_anyone.check_delivery_status(args=[deal_id, now + 15]).transact(
        wait_interval=8000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("check_delivery_status:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    assert deal["tracking_checked"] is True
    print("tracking_status:", deal["tracking_status"])

    receipt = c_anyone.resolve_condition(args=[deal_id, now + 20]).transact(
        wait_interval=8000, wait_retries=120
    )
    assert not tx_execution_failed(receipt), receipt
    print("resolve_condition:", receipt.get("status_name"))

    deal = c_anyone.get_deal(args=[deal_id]).call()
    print("post-adjudication state:", json.dumps(deal, indent=2))
    assert deal["status"] in ("VERDICT_PENDING", "UNDETERMINED")
    assert deal["price_deposited_wei"] == 1 * GEN  # untouched pre-finalization

    if deal["status"] == "UNDETERMINED":
        pytest.skip("adjudication returned UNDETERMINED on this run; see honest-limits in README")

    receipt = c_anyone.finalize_deal(args=[deal_id, deal["resolved_ts"] + 172800 + 1]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("finalize_deal:", receipt.get("status_name"))

    final_deal = c_anyone.get_deal(args=[deal_id]).call()
    print("FINAL state:", json.dumps(final_deal, indent=2))
    assert final_deal["status"].startswith("FINALIZED")
    assert final_deal["price_deposited_wei"] == 0
    assert final_deal["seller_bond_deposited_wei"] == 0

    stats = c_anyone.get_platform_stats().call()
    print("platform stats:", json.dumps(stats, indent=2))
    assert stats["total_finalized"] >= 1

    activity = c_anyone.get_activity(args=[deal_id]).call()
    kinds = [a["kind"] for a in activity]
    print("activity kinds:", kinds)
    assert "CREATE" in kinds
    assert "ACCEPT" in kinds
    assert "DELIVERY_SUBMITTED" in kinds
    assert "FINALIZED" in kinds


@pytest.mark.integration
def test_convergence_condition_adjudication_is_deterministic_given_same_evidence():
    """Convergence property: two independent calls to resolve_condition for
    two separate deals funded with IDENTICAL listing/delivery photos and
    description must reach the SAME verdict band — the strict form of the
    property this primitive depends on validators agreeing about."""
    buyer = _load_account("buyer")
    seller = _load_account("seller")
    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)
    c_seller = factory.build_contract(CONTRACT_ADDRESS, account=seller)

    listing_url = "https://picsum.photos/seed/dvault-convergence-listing/800/600.jpg"
    delivery_url = "https://picsum.photos/seed/dvault-convergence-listing/800/600.jpg"  # identical photo

    bands = []
    for i in range(2):
        now = int(time.time())
        ship_by = now + 120
        deliver_by = ship_by + 120
        receipt = c_buyer.create_deal(
            args=[
                seller.address,
                "Convergence check item",
                "Identical listing and delivery photo for a strict convergence test",
                listing_url,
                "https://httpbin.org/status/200",
                ship_by,
                deliver_by,
                now,
            ]
        ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
        assert not tx_execution_failed(receipt), receipt
        deal_id = c_buyer.get_deal_count().call() - 1

        c_seller.accept_deal(args=[deal_id, now + 5]).transact(
            wait_interval=5000, wait_retries=90
        )
        c_buyer.submit_delivery_evidence(args=[deal_id, delivery_url, now + 10]).transact(
            wait_interval=5000, wait_retries=90
        )
        receipt = c_buyer.resolve_condition(args=[deal_id, now + 20]).transact(
            wait_interval=8000, wait_retries=120
        )
        assert not tx_execution_failed(receipt), receipt
        deal = c_buyer.get_deal(args=[deal_id]).call()
        print(f"run {i}: band={deal['verdict_band']} status={deal['status']}")
        bands.append(deal["verdict_band"])

    assert bands[0] == bands[1], f"convergence failed: {bands}"
    assert bands[0] == "MATCH", f"identical photos should converge on MATCH, got {bands}"
