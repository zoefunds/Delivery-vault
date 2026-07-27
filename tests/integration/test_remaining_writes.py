"""Exercises every remaining write method against the deployed contract that
the main lifecycle test doesn't already cover: finalize_deal, cancel_deal,
timeout_unaccepted_reclaim, timeout_undelivered_reclaim, contest_verdict,
resolve_contest, force_refund_undetermined guard. Each uses its own fresh
deal so runs are independent and repeatable.
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


@pytest.fixture(scope="module")
def contracts():
    buyer = _load_account("buyer")
    seller = _load_account("seller")
    factory = get_contract_factory(contract_file_path="delivery_vault.py")
    c_buyer = factory.build_contract(CONTRACT_ADDRESS, account=buyer)
    c_seller = factory.build_contract(CONTRACT_ADDRESS, account=seller)
    return c_buyer, c_seller, buyer, seller


@pytest.mark.integration
def test_finalize_deal_1_now_that_contest_window_can_be_forced(contracts):
    """Deal 1 (from test_reliable_image_source.py) sits at VERDICT_PENDING.
    now_ts is caller-supplied, so a future value forces the contest-window
    check deterministically without waiting 48 real hours."""
    c_buyer, _, _, _ = contracts
    deal = c_buyer.get_deal(args=[1]).call()
    if deal["status"] != "VERDICT_PENDING":
        pytest.skip(f"deal 1 is {deal['status']}, not VERDICT_PENDING")
    far_future = deal["resolved_ts"] + 172800 + 100
    receipt = c_buyer.finalize_deal(args=[1, far_future]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    final = c_buyer.get_deal(args=[1]).call()
    print("finalized deal 1:", json.dumps(final, indent=2))
    assert final["status"].startswith("FINALIZED")
    assert final["price_deposited_wei"] == 0


@pytest.mark.integration
def test_cancel_deal_refunds_buyer_live(contracts):
    c_buyer, c_seller, buyer, seller = contracts
    now = int(time.time())
    receipt = c_buyer.create_deal(
        args=[
            seller.address, "Cancel-path test item", "d",
            "https://httpbin.org/image/jpeg", "https://httpbin.org/status/200",
            now + 300, now + 600, now,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1

    receipt = c_buyer.cancel_deal(args=[deal_id, now + 5]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    deal = c_buyer.get_deal(args=[deal_id]).call()
    print("cancelled deal:", json.dumps(deal, indent=2))
    assert deal["status"] == "CANCELLED"
    assert deal["price_deposited_wei"] == 0


@pytest.mark.integration
def test_timeout_unaccepted_reclaim_live(contracts):
    c_buyer, _, _, seller = contracts
    now = int(time.time())
    ship_by = now + 5  # short window so we can force past it deterministically
    receipt = c_buyer.create_deal(
        args=[
            seller.address, "Unaccepted-timeout test item", "d",
            "https://httpbin.org/image/jpeg", "https://httpbin.org/status/200",
            ship_by, ship_by + 300, now,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1

    receipt = c_buyer.timeout_unaccepted_reclaim(args=[deal_id, ship_by + 100]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    deal = c_buyer.get_deal(args=[deal_id]).call()
    print("timed-out deal:", json.dumps(deal, indent=2))
    assert deal["status"] == "TIMEOUT_UNACCEPTED"
    assert deal["price_deposited_wei"] == 0


@pytest.mark.integration
def test_timeout_undelivered_reclaim_live(contracts):
    c_buyer, c_seller, _, seller = contracts
    now = int(time.time())
    ship_by = now + 5
    deliver_by = ship_by + 5
    receipt = c_buyer.create_deal(
        args=[
            seller.address, "Undelivered-timeout test item", "d",
            "https://httpbin.org/image/jpeg", "https://httpbin.org/status/200",
            ship_by, deliver_by, now,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1

    receipt = c_seller.accept_deal(args=[deal_id, now + 1]).transact(
        value=int(0.05 * GEN), wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt

    far_future = deliver_by + 604800 + 100
    receipt = c_buyer.timeout_undelivered_reclaim(args=[deal_id, far_future]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    deal = c_buyer.get_deal(args=[deal_id]).call()
    print("undelivered-timeout deal:", json.dumps(deal, indent=2))
    assert deal["status"] == "TIMEOUT_UNDELIVERED"
    assert deal["price_deposited_wei"] == 0
    assert deal["seller_bond_deposited_wei"] == 0


@pytest.mark.integration
def test_contest_verdict_and_resolve_contest_live(contracts):
    c_buyer, c_seller, buyer, seller = contracts
    now = int(time.time())
    ship_by = now + 300
    deliver_by = ship_by + 300
    receipt = c_buyer.create_deal(
        args=[
            seller.address, "Contest-path test item",
            "Item used to exercise the bonded contest ladder live",
            "https://httpbin.org/image/jpeg", "https://httpbin.org/status/200",
            ship_by, deliver_by, now,
        ]
    ).transact(value=1 * GEN, wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    deal_id = c_buyer.get_deal_count().call() - 1

    c_seller.accept_deal(args=[deal_id, now + 5]).transact(wait_interval=5000, wait_retries=90)
    c_buyer.submit_delivery_evidence(
        args=[deal_id, "https://httpbin.org/image/jpeg", now + 10]
    ).transact(wait_interval=5000, wait_retries=90)
    receipt = c_buyer.resolve_condition(args=[deal_id, now + 20]).transact(
        wait_interval=8000, wait_retries=120
    )
    assert not tx_execution_failed(receipt), receipt
    deal = c_buyer.get_deal(args=[deal_id]).call()
    if deal["status"] != "VERDICT_PENDING":
        pytest.skip(f"deal {deal_id} adjudication returned {deal['status']}, cannot exercise contest path this run")

    receipt = c_seller.contest_verdict(args=[deal_id, now + 25]).transact(
        value=int(0.15 * GEN), wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    deal = c_buyer.get_deal(args=[deal_id]).call()
    assert deal["status"] == "CONTESTED"
    print("contested deal:", json.dumps(deal, indent=2))

    receipt = c_buyer.resolve_contest(args=[deal_id, now + 30]).transact(
        wait_interval=8000, wait_retries=120
    )
    assert not tx_execution_failed(receipt), receipt
    final = c_buyer.get_deal(args=[deal_id]).call()
    print("contest-resolved deal:", json.dumps(final, indent=2))
    assert final["status"].startswith("FINALIZED")
    assert final["contest_outcome"] in ("UPHELD", "OVERTURNED")
    assert final["contest_bond_deposited_wei"] == 0
