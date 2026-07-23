from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from deal_tracker.platform.enums import ListingStatus, OfferStatus
from deal_tracker.platform.models import CatalogueListing, Offer


def ensure_private_listing(
    session: Session,
    offer: Offer,
) -> CatalogueListing | None:
    """Materialise an approved, priced offer without exposing it to agencies."""

    if offer.status != OfferStatus.APPROVED or offer.reseller_price_aud is None:
        return None

    listing = session.scalar(
        select(CatalogueListing).where(CatalogueListing.offer_id == offer.id)
    )
    if listing is None:
        listing = CatalogueListing(
            offer_id=offer.id,
            publisher_id=offer.publisher_id,
            domain_id=offer.domain_id,
            status=ListingStatus.PRIVATE,
            visibility_tier="private",
            agency_price_aud=offer.reseller_price_aud,
            listed_at=None,
        )
        session.add(listing)
    elif listing.status != ListingStatus.LISTED:
        listing.status = ListingStatus.PRIVATE
        listing.visibility_tier = "private"
        listing.agency_price_aud = offer.reseller_price_aud
        listing.listed_at = None
    return listing
