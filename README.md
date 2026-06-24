# Facebook Marketing API — integration code sample

A sanitized, self-contained excerpt of the Facebook/Marketing API layer of an
internal, first-party marketing-operations app, provided for **Meta App Review**.

## What the app does
The app manages advertising on a **single owned Facebook ad account** using a
**system-user token**. It is strictly internal — it does **not** access any
third-party or end-user data, and it does not manage accounts outside our own
business. It programmatically runs monthly promotional campaigns across the
locations we own and operate:

- **Creates** a Campaign per promotional offer (objective, schedule).
- **Creates** a geo-targeted **Ad Set** per location (local radius + age targeting),
  lifetime budget.
- **Uploads** creative images (`/adimages`) and builds **AdCreatives**, then creates
  **Ads** (one per location × active creative).
- **Reads** object status and generates **ad previews** (`generatepreviews`) for
  internal review.
- **Updates** ads in place (budget, schedule, creative repoint; pause/resume).
- **Deletes** ad sets/ads when a location opts out.

A single campaign can span 100–300 ad sets, 200–600 ad creatives, and 200–600 ads,
which is why higher rate limits (Advanced Access) are required.

## Files
- **`facebook_api.py`** — the Graph/Marketing API layer: low-level POST/GET/UPDATE/
  DELETE helpers (with error + rate-limit-header handling), payload builders for
  Campaigns/Ad Sets/Ads, image upload, AdCreative creation, and preview generation.
- **`facebook_creative_compiler.py`** — a pure function that compiles an internal
  creative definition into a Facebook AdCreative payload (uniform / multi-image /
  per-placement; site links; Advantage+ degrees-of-freedom specs).

## Notes
- This excerpt removes database persistence, idempotency bookkeeping, and the web
  framework so the API usage reads clearly in isolation.
- **No secrets are committed.** All credentials come from environment variables
  (`FACEBOOK_ACCESS_TOKEN`, `FACEBOOK_PREVIEW_ACCESS_TOKEN`, `FACEBOOK_AD_ACCOUNT_ID`,
  `FACEBOOK_API_VERSION`, `FACEBOOK_PIXEL_ID`). Account-specific identifiers (page id,
  site-link image hashes) are placeholders marked `REPLACE_ME`.
- Writes are gated by `FACEBOOK_WRITES_ENABLED`; unset → dry-run (no API traffic).

## Run the dry-run demo
```bash
pip install -r requirements.txt
python facebook_api.py    # FACEBOOK_WRITES_ENABLED unset → logs the calls, no API traffic
```
