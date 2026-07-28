# README Assets

This directory contains sanitized screenshots for the README files.

The current PNGs are rendered from the real admin web with intercepted,
fictional fixtures. The deduplication preview uses programmatically generated
geometric placeholders rather than downloaded media. Every asset in this
directory must remain sanitized: no private creators, cookies, local paths,
account names, or downloaded media that should not be public.

Expected assets:

- `admin-dashboard.png`
- `tag-bubbles.png`
- `asset-dedup-review.png`

Before replacing an image, verify the fixture routes intercept every API
request, inspect the final pixels, and run the repository privacy scan.
