# System Map

AU Link Desk has two connected halves.

## Prospecting

The prospecting system finds domains likely to accept paid guest posts, sponsored articles, advertorials, media packages, or link insertions.

Discovery routes:

- Search operators such as `site:.com.au "write for us"`, `intitle:"media kit"`, `inurl:advertise`, and Australian-context `.com` searches.
- Competitor backlink/source-domain exports from SE Ranking, Semrush, Ahrefs, or CSV.
- Contact discovery through homepages, known paths, sitemaps, robots files, same-site links, public search, and role-email ranking.

Key outputs:

- `prospecting/generated/search_harvest/*.csv`
- `prospecting/generated/guest_post_prospecting/paid_link_acceptance_review.csv`
- `deal-tracker/generated/imports/combined_filtered_link_prospects_review.csv`
- `deal-tracker/generated/imports/combined_link_prospects_contact_discovery_review.csv`
- `deal-tracker/generated/imports/likely_guest_post_candidates_strict.csv`

## Outreach

Approved leads are pushed to Instantly with the subject `Article Submission Info Request`.

Lead imports include:

- Email and website
- Root domain and site name
- Source URL/evidence
- Opportunity type
- Niche/category
- Custom variables for personalization

Campaigns stay reviewable before launch and use duplicate protection.

## Deal Tracker

The deal tracker syncs Instantly replies and stores any reply that gives pricing, requirements, rate cards, or a clear paid-placement path.

Admin inventory tracks:

- Publisher cost and reseller price
- Link insertion cost
- Domain Trust
- Placement type
- Writing and link requirements
- Turnaround
- Restrictions
- Raw reply evidence
- Publisher entity
- Listing/review status

Agency users only see approved public catalogue fields according to their visibility tier.

## Feedback Loop

Replies improve future prospecting:

- Rejections become suppression/exclusion signals.
- Network replies create multiple publisher rows under one entity.
- Corrected dofollow/nofollow pricing updates inventory rules.
- Agency demand highlights niches for new prospecting runs.
