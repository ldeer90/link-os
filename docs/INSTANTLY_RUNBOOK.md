# Instantly Runbook

## Environment

Set:

```text
INSTANTLY_API_KEY=
```

API base:

```text
https://api.instantly.ai/api/v2
```

Default campaigns currently used by the tracker:

- Main: `7bc7d5e3-924d-463b-b875-8e8301be264b`
- Priority: `48508c88-901d-4ba7-963b-031514a9ef73`
- Old paused: `945b0afd-7c53-492d-9afe-4c44f57173ad`

## Lead Import

Use approved review CSVs only. Before import, remove domains already in:

- Instantly workspace
- Active campaigns
- Deal tracker inventory
- Suppression/exclusion files

Recommended lead payload:

```json
{
  "campaign_id": "CAMPAIGN_ID",
  "leads": [
    {
      "email": "editor@example.com.au",
      "first_name": "there",
      "company_name": "Example Site",
      "website": "https://example.com.au",
      "custom_variables": {
        "root_domain": "example.com.au",
        "site_name": "Example Site",
        "opportunity_type": "sponsored_post",
        "source_url": "https://example.com.au/advertise",
        "niche": "lifestyle",
        "contact_email_type": "advertising"
      }
    }
  ],
  "verify_leads_on_import": false,
  "skip_if_in_workspace": true,
  "skip_if_in_campaign": true
}
```

## Campaign Defaults

Subject:

```text
Article Submission Info Request
```

Sending defaults:

- 30/day for normal batches
- Business hours 8am-8pm
- Weekend sending is allowed when configured
- Keep copy line spacing consistent with SEO audit campaigns

## Sync Replies

```bash
docker compose run --rm deal-tracker python tools/sync_instantly_replies_to_deals.py
```

The sync stores inbound replies in `instantly_reply_sync` and creates deal rows only when the reply includes pricing, rate cards, requirements, or a clear paid-placement process.

## Positive Reply Fallback

Sometimes Instantly sends a positive-reply notification to Outlook before the API sync sees the body. Search Outlook/Gmail for:

```text
Instantly positive reply
Article Submission Info Request
may have sent a positive reply
```

If a useful notification has pricing, manually add it to the deal tracker with the notification body as evidence.

## Pricing Rule

For SEO/link-building inventory, use dofollow pricing as the primary publisher cost. Keep cheaper sponsored/nofollow options in notes, not as the main cost.
