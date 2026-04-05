# License Fulfillment

## Customer Flow

The intended public flow is:

1. The user opens `STS Managed Controls`.
2. The user opens `License -> License Status...`.
3. The UI shows the current `Install ID`.
4. The user clicks `Purchase Page`.
5. The checkout page receives the `install_id` in the URL.
6. After payment, the customer receives a signed activation key.
7. The customer pastes the key into the UI or runs:

```powershell
python -m sts_bot.cli activate-managed-controls --license-key <KEY>
```

## App-Side Purchase URL Config

Set one of these:

- environment variable: `STS_MANAGED_CONTROLS_PURCHASE_URL`
- local file: `.managed_controls\commerce.json`

Example:

```json
{
  "purchase_url": "https://your-checkout-url.example/checkout",
  "activation_guide_url": "https://github.com/Ranats/sts2-managed-controls#trial-and-unlock",
  "support_url": "mailto:you@example.com"
}
```

There is also a ready-to-copy example file:

- `docs/release/commerce.example.json`

If the purchase URL is a Lemon Squeezy checkout URL, the app automatically appends both:

- `install_id`
- `checkout[custom][install_id]`

That makes it easier to recover the install id in webhook automation.

## Seller Flow

The repo now includes two issuance paths:

- one-off CLI issuance
- a small HTTP issuer service for webhook or automation use

Manual issuance:

```powershell
python -m sts_bot.cli issue-managed-controls-license --install-id <INSTALL_ID> --licensee "Customer Name" --private-key-file .managed_controls\issuer_private_key.pem --days 365
```

Automation-friendly issuer service:

```powershell
python -m sts_bot.cli serve-managed-controls-issuer --private-key-file .managed_controls\issuer_private_key.pem --admin-token <SECRET>
```

`POST /issue` expects:

```json
{
  "install_id": "SMC-1234567890ABCDEF",
  "licensee": "Customer Name",
  "plan": "standard",
  "days": 365
}
```

with:

- header `Authorization: Bearer <SECRET>`

Response:

```json
{
  "ok": true,
  "install_id": "SMC-1234567890ABCDEF",
  "licensee": "Customer Name",
  "plan": "standard",
  "expires_at": "2027-04-05T00:00:00Z",
  "license_key": "SMC2...."
}
```

## Recommended Payment Setup

The recommended setup for this project is:

1. Lemon Squeezy hosted checkout
2. `auth.cilabworks.com` running the fulfillment service
3. Lemon Squeezy webhook to `POST /webhooks/lemonsqueezy`
4. Resend sends the activation key email

Why this is the best fit here:

- Lemon Squeezy is lighter than a full custom Stripe checkout for a downloadable single-product tool
- it already supports hosted checkout and webhook delivery with custom checkout data
- `auth.cilabworks.com` can stay focused on activation and fulfillment rather than acting as the payment UI itself
- you already have a verified email domain, so delivering the activation key by email is straightforward

Suggested hosted layout:

- `auth.cilabworks.com/health`
- `auth.cilabworks.com/webhooks/lemonsqueezy`
- optional `auth.cilabworks.com/activate` guide page

Run the hosted service with:

```powershell
python -m sts_bot.cli serve-managed-controls-fulfillment --private-key-file .managed_controls\issuer_private_key.pem --admin-token <SECRET> --host 0.0.0.0 --port 8787
```

Required config:

- `purchase_url`
- `lemonsqueezy_webhook_secret`
- `resend_api_key`
- `email_from`

Resend sends the activation key to the buyer email from the order payload.

The older `serve-managed-controls-issuer` command still works for internal manual issuance or automation, but the fulfillment service is the main production entrypoint.

Alternative:

- Stripe Payment Link + webhook + the same fulfillment service model

Stripe remains a valid fallback, but for this project Lemon Squeezy is the better first implementation.
