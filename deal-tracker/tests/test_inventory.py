from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from deal_tracker.inventory import ensure_private_listing
from deal_tracker.platform.enums import ListingStatus, OfferStatus
from deal_tracker.platform.models import Base, CatalogueListing, Domain, Offer, Reply, utcnow


def _session_and_offer(*, status: OfferStatus, reseller_price: Decimal | None):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    session = Session()
    domain = Domain(
        normalized_domain="publisher.example",
        first_input_value="publisher.example",
        tld="example",
    )
    session.add(domain)
    session.flush()
    reply = Reply(
        external_email_id="reply-1",
        domain_id=domain.id,
        received_at=utcnow(),
    )
    session.add(reply)
    session.flush()
    offer = Offer(
        reply_id=reply.id,
        domain_id=domain.id,
        status=status,
        placement_type="guest_post",
        original_amount=Decimal("100"),
        original_currency="AUD",
        reseller_price_aud=reseller_price,
    )
    session.add(offer)
    session.flush()
    return session, offer


def test_approved_priced_offer_creates_private_inventory() -> None:
    session, offer = _session_and_offer(
        status=OfferStatus.APPROVED,
        reseller_price=Decimal("180"),
    )

    listing = ensure_private_listing(session, offer)
    session.flush()

    assert listing is not None
    assert listing.status == ListingStatus.PRIVATE
    assert listing.visibility_tier == "private"
    assert listing.agency_price_aud == Decimal("180")
    assert listing.listed_at is None
    assert session.scalar(
        select(CatalogueListing).where(CatalogueListing.offer_id == offer.id)
    ) is listing


def test_private_inventory_materialisation_is_idempotent_and_preserves_listed_rows() -> None:
    session, offer = _session_and_offer(
        status=OfferStatus.APPROVED,
        reseller_price=Decimal("180"),
    )
    listing = ensure_private_listing(session, offer)
    session.flush()
    assert listing is not None
    listing.status = ListingStatus.LISTED
    listing.visibility_tier = "basic"
    listing.listed_at = utcnow()
    session.flush()

    same_listing = ensure_private_listing(session, offer)
    session.flush()

    assert same_listing is listing
    assert listing.status == ListingStatus.LISTED
    assert listing.visibility_tier == "basic"
    assert session.query(CatalogueListing).count() == 1


def test_unapproved_or_unpriced_offer_does_not_create_inventory() -> None:
    review_session, review_offer = _session_and_offer(
        status=OfferStatus.REVIEW,
        reseller_price=Decimal("180"),
    )
    unpriced_session, unpriced_offer = _session_and_offer(
        status=OfferStatus.APPROVED,
        reseller_price=None,
    )

    assert ensure_private_listing(review_session, review_offer) is None
    assert ensure_private_listing(unpriced_session, unpriced_offer) is None
    assert review_session.query(CatalogueListing).count() == 0
    assert unpriced_session.query(CatalogueListing).count() == 0
