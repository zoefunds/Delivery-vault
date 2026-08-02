"""Direct tests for the worked consumer example. Confirms it contains none
of the vault's machinery and only forwards value + bookkeeping."""

from pathlib import Path

import pytest
from gltest.direct import VMContext, deploy_contract, create_address

CONTRACT = Path(__file__).parent.parent.parent / "examples" / "marketplace_board.py"

SELLER = create_address("seller")
BUYER = create_address("buyer")
GEN = 10**18


def fresh(vm: VMContext):
    vm.sender = SELLER
    vm.value = 0
    return deploy_contract(CONTRACT, vm)


def test_post_and_get_listing():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = SELLER
        lid = c.post_listing("Vintage bicycle")
        assert lid == 0
        listing = c.get_listing(0)
        assert listing["title"] == "Vintage bicycle"
        assert listing["has_deal"] is False


def test_buy_listing_marks_funded_without_reverting():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = SELLER
        c.post_listing("Vintage bicycle")
        vm.sender = BUYER
        vm.value = 1 * GEN
        c.buy_listing(0, "steel frame, 21-speed", "https://x.test/l.jpg", "https://x.test/t", 1, 2)
        vm.value = 0
        assert c.get_listing(0)["has_deal"] is True


def test_buy_listing_rejects_unknown_listing():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = BUYER
        vm.value = 1 * GEN
        with vm.expect_revert():
            c.buy_listing(99, "d", "https://x.test/l.jpg", "https://x.test/t", 1, 2)
        vm.value = 0


def test_buy_listing_rejects_double_funding():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = SELLER
        c.post_listing("Vintage bicycle")
        vm.sender = BUYER
        vm.value = 1 * GEN
        c.buy_listing(0, "d", "https://x.test/l.jpg", "https://x.test/t", 1, 2)
        with vm.expect_revert():
            c.buy_listing(0, "d", "https://x.test/l.jpg", "https://x.test/t", 1, 2)
        vm.value = 0


def test_record_deal_id_only_seller():
    vm = VMContext()
    with vm.activate():
        c = fresh(vm)
        vm.sender = SELLER
        c.post_listing("Vintage bicycle")
        vm.sender = BUYER
        with vm.expect_revert():
            c.record_deal_id(0, 7)
        vm.sender = SELLER
        c.record_deal_id(0, 7)
        assert c.get_listing(0)["vault_deal_id"] == 7
