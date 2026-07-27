# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""Worked consumer example: a tiny listing board that imports the
DeliveryVault primitive to fund escrowed trades, and contains NONE of the
vault's own machinery (no image adjudication, no tracking fetch, no contest
ladder, no escrow ledger). It only stores which vault deal id belongs to
which listing and forwards value into the vault.

This is the whole integration surface a consumer needs:
"""

from dataclasses import dataclass

from genlayer import *


VAULT_ADDRESS = "0xd661bea0F9796CA39d8bA4BBe5cF09E7C7138758"  # StudioNet deployment


@gl.contract_interface
class DeliveryVault:
    class View:
        def get_deal(self, deal_id: int) -> dict: ...
        def get_deal_summary(self, deal_id: int) -> dict: ...

    class Write:
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
        ) -> int: ...

        def accept_deal(self, deal_id: int, now_ts: int) -> None: ...


@allow_storage
@dataclass
class Listing:
    seller: Address
    title: str
    vault_deal_id: u32
    has_deal: bool


class MarketplaceBoard(gl.Contract):
    """A minimal listing board: sellers post items, buyers fund a trade
    through the DeliveryVault, and the board just remembers which vault deal
    corresponds to which listing. All escrow, evidence, and dispute logic
    lives entirely in the vault - this contract does not duplicate any of
    it."""

    listing_count: u32
    listings: TreeMap[u32, Listing]

    def __init__(self):
        self.listing_count = u32(0)

    @gl.public.write
    def post_listing(self, title: str) -> int:
        listing_id = int(self.listing_count)
        self.listing_count = u32(listing_id + 1)
        self.listings[u32(listing_id)] = Listing(
            seller=gl.message.sender_address,
            title=title.strip()[:160],
            vault_deal_id=u32(0),
            has_deal=False,
        )
        return listing_id

    @gl.public.write.payable
    def buy_listing(
        self,
        listing_id: int,
        item_description: str,
        listing_photo_url: str,
        tracking_url: str,
        ship_by_ts: int,
        deliver_by_ts: int,
        now_ts: int,
    ) -> None:
        """Fund escrow for a listing through the vault. Any GEN attached
        here is forwarded as the vault's escrowed price on finalization - the
        board never holds funds itself. A value-carrying cross-contract call
        is a deferred message (see gotcha #3 in the vault's escrow value
        rules), so the resulting deal id is not known synchronously here;
        the buyer reads it off `DeliveryVault.get_party_deal_ids(buyer)` and
        records it with `record_deal_id` below for the board's own bookkeeping."""
        listing = self.listings.get(u32(listing_id))
        if listing is None:
            raise gl.vm.UserError("EXPECTED: listing does not exist")
        if listing.has_deal:
            raise gl.vm.UserError("EXPECTED: listing already has a funded deal")

        vault = gl.get_contract_at(Address(VAULT_ADDRESS))
        vault.emit(value=gl.message.value, on="finalized").create_deal(
            listing.seller.as_hex,
            listing.title,
            item_description,
            listing_photo_url,
            tracking_url,
            ship_by_ts,
            deliver_by_ts,
            now_ts,
        )
        listing.has_deal = True

    @gl.public.write
    def record_deal_id(self, listing_id: int, vault_deal_id: int) -> None:
        """Bookkeeping only: the buyer or seller records the vault's deal id
        (read from the vault's own views) against this listing once known."""
        listing = self.listings.get(u32(listing_id))
        if listing is None:
            raise gl.vm.UserError("EXPECTED: listing does not exist")
        sender = gl.message.sender_address
        if sender != listing.seller:
            raise gl.vm.UserError("EXPECTED: only the seller may record the deal id")
        listing.vault_deal_id = u32(int(vault_deal_id))

    @gl.public.view
    def get_listing(self, listing_id: int) -> dict:
        listing = self.listings.get(u32(listing_id))
        if listing is None:
            raise gl.vm.UserError("EXPECTED: listing does not exist")
        return {
            "seller": listing.seller.as_hex,
            "title": listing.title,
            "vault_deal_id": int(listing.vault_deal_id),
            "has_deal": bool(listing.has_deal),
        }

    @gl.public.view
    def get_listing_count(self) -> int:
        return int(self.listing_count)
