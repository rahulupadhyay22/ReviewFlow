# Google Business Profile — Verified Provider Facts

Provider facts for spec `09-google-business-profile` (gate items G-1 …
G-15). Every row cites the official Google page it was read from and the
date it was read. The spec asserts nothing about Google that is not in
this table.

- **VERIFIED**: the cited official page states it.
- **PARTIAL**: the page states part of it; the rest is listed as open.
- **UNVERIFIED**: no official statement found. Code that depends on it
  stays unwritten (or the dependent decision stays blocked).

Verification date for every row: **2026-10-23**. "Page date" is the
"Last updated" footer of the cited page, where one was shown.

## Gate table

| # | Item | Status | Finding | Source (page date) |
|---|---|---|---|---|
| G-1 | API access approval and default quota | VERIFIED | Access is requested through the GBP API contact form ("Application for Basic API Access"). The applicant must "Manage a Google Business Profile that is verified and active for 60+ days". "If your quota is 0 QPM … your project has not yet been approved"; "If your quota is set to 300 QPM, your project is approved." After approval, the Business Profile APIs are enabled for the project. | https://developers.google.com/my-business/content/prereqs (2026-08-28) |
| G-2 | OAuth endpoints | VERIFIED | Authorization `https://accounts.google.com/o/oauth2/v2/auth` (HTTPS only). Code exchange and refresh: `POST https://oauth2.googleapis.com/token`. Revocation: `https://oauth2.googleapis.com/revoke`. The redirect URI must exactly match a registered one (scheme, case, trailing slash), must be HTTPS except localhost, and has no fragment. The `state` returned by Google should be confirmed to match the one sent. | https://developers.google.com/identity/protocols/oauth2/web-server (2026-10-16) |
| G-3 | Access-token lifetime | PARTIAL | The token response carries `expires_in`, "the remaining lifetime of the access token in seconds". The general page says only "Access tokens have limited lifetimes"; no fixed duration is documented. **Consequence:** `expires_at` must be computed from `expires_in` on every exchange and refresh. No fixed lifetime may be assumed, so sync must refresh before calling when the token is near expiry. | web-server page (2026-10-16); https://developers.google.com/identity/protocols/oauth2 (2026-10-16) |
| G-4 | Authorization-code lifetime | **UNVERIFIED** | Neither page states a lifetime for the authorization code, or whether it is single-use. The only related text is the error for a code that "is invalid or in the wrong format". | web-server page (2026-10-16) |
| G-5 | Minimum scopes | VERIFIED | `accounts.list`, `accounts.locations.list` and `accounts.locations.reviews.list` each accept `https://www.googleapis.com/auth/business.manage` (reviews also accept `plus.business.manage`). The token response's `scope` field lists the scopes actually granted. | accountmanagement `accounts/list` (2024-10-16); businessinformation `accounts.locations/list` (2024-10-16); v4 `accounts.locations.reviews/list` (2026-04-07); web-server page |
| G-6 | Stable account identifier and email | VERIFIED | With scope `openid email`, the ID token carries `sub`: "An identifier for the user, unique among all Google Accounts and never reused. Use `sub` within your application as the unique-identifier key for the user." The `email` "could change over time … you shouldn't use this value as the primary identifier". `email_verified` is also included. An ID token received directly from the token endpoint over HTTPS with the client secret can be trusted as coming from Google; it must be validated if passed to other components. The server flow's step "Exchange code for access token and ID token" uses `https://oauth2.googleapis.com/token`, and its response includes `id_token`: "A JWT that contains identity information about the user that is digitally signed by Google." | https://developers.google.com/identity/openid-connect/openid-connect (2026-06-15) |
| G-7 | Getting a refresh token | PARTIAL | `access_type=offline` "instructs the Google authorization server to return a refresh token *and* an access token the first time that your application exchanges an authorization code for tokens". "The `refresh_token` is only returned on the first authorization." To obtain a new one when one exists, the user must revoke the grant or the app must use `prompt=consent` in the authorization request. The refresh response sample contains `access_token`, `expires_in`, `scope`, `token_type` and **no** `refresh_token`. **Open:** the page does not state outright that a refresh response never carries a new refresh token. | web-server page (2026-10-16) |
| G-8 | Account and location listing | VERIFIED | Accounts: `GET https://mybusinessaccountmanagement.googleapis.com/v1/accounts`, `pageSize` default and max 20, `nextPageToken` present only when more exist; names are `accounts/{account_id}`. Locations: `GET https://mybusinessbusinessinformation.googleapis.com/v1/{parent=accounts/*}/locations`, `readMask` **required**, `pageSize` default 10 and max 100, `nextPageToken` only when more exist. A location's `name` is `locations/{locationId}`. | accountmanagement `accounts/list` (2024-10-16); businessinformation `accounts.locations/list` (2024-10-16) |
| G-9 | Place ID on a location | PARTIAL | `Location.metadata.placeId`: "Output only. If this location appears on Google Maps, this field is populated with the place ID for the location." So it can be absent; the page does not list the conditions beyond "appears on Google Maps". The same `metadata` also has `newReviewUri`: "A link to the page on Google Search where a customer can leave a review for the location" (see G-13). | businessinformation `accounts.locations` resource (2024-10-16) |
| G-10 | Revocation vs transient errors; refresh-token expiry | PARTIAL | `invalid_grant` on refresh: "the token may have expired or has been invalidated"; the fix is to "Authenticate the user again". A refresh token stops working when: the user revoked access; it was unused for six months; the account exceeded the maximum number of live refresh tokens (100 per Google Account per client ID); time-based access expired; an admin restricted a requested scope (`admin_policy_enforced`); an admin session length was exceeded. An OAuth client with publishing status "Testing" gets refresh tokens "expiring in 7 days". **Open:** the exact HTTP status and body of `invalid_grant` on the token endpoint, and which errors are transient, are not shown in the read text. | web-server page (2026-10-16); https://developers.google.com/identity/protocols/oauth2 (2026-10-16) |
| G-11 | Reviews list | VERIFIED | `GET https://mybusiness.googleapis.com/v4/{parent=accounts/*/locations/*}/reviews`. Only valid for a **verified** location. `pageSize` max 50; `pageToken`; `orderBy` default `updateTime desc` (allowed `rating`, `rating desc`, `updateTime desc`). Response: `reviews[]`, `averageRating`, `totalReviewCount`, `nextPageToken` (absent on the last page). Review fields: `name` (`accounts/{accountId}/locations/{locationId}/reviews/{reviewId}`), `reviewId` ("The encrypted unique identifier"), `reviewer.displayName` (real name only if `isAnonymous` is false), `reviewer.isAnonymous`, `starRating` (enum `STAR_RATING_UNSPECIFIED`, `ONE` … `FIVE`), `comment`, `createTime`, `updateTime`, `reviewReply`. **Note:** the v4 parent needs the **account** id as well as the location id, while the Business Information API names a location `locations/{locationId}` only. | v4 `accounts.locations.reviews/list` (2026-04-07); v4 `accounts.locations.reviews` resource (2026-07-24) |
| G-12 | Rate limits | PARTIAL | Default 300 QPM for the Business Information, Account Management and other listed APIs. Over quota: "the API responds with a `429 Too Many Requests` HTTP status code". Google recommends exponential backoff with jitter. **No `Retry-After` header is documented.** The page does not explicitly say quotas are per project (the quota-increase form asks for a project number). The v4 reviews API's quota is not listed on that page. | https://developers.google.com/my-business/content/limits (2026-08-28) |
| G-13 | Review-link format `https://search.google.com/local/writereview?placeid=<place_id>` | **UNVERIFIED** | No official Google page documenting this URL format was found. The GBP Help page on getting reviews says customers can "visit a Google link or scan a QR code" without giving a format. The only officially documented review link found is the API's `metadata.newReviewUri` (G-9). | https://support.google.com/business/answer/3474122 (no date shown); search of developers.google.com / support.google.com |
| G-15 | ID-token validation | VERIFIED | "Validating an ID token" lists: verify the signature with "one of the certificates found at the URI specified in the `jwks_uri`"; `iss` is `https://accounts.google.com` or `accounts.google.com`; `aud` equals the app's client ID; `exp` has not passed (claims table: must not be accepted "on or after this time"); `hd` only if an `hd` parameter was sent. Keys may be cached per the HTTP response's cache directives. The `tokeninfo` endpoint is for debugging; requests to it "may be throttled or otherwise subject to intermittent errors". `nonce`: the authentication-request table marks it required (the step list says "when present", which is inconsistent); the claim echoes the supplied value, and a nonce should be accepted only once. `azp` matters only when the requesting party differs from the audience. `sub` is up to 255 ASCII characters. Discovery document: `issuer` `https://accounts.google.com`, `jwks_uri` `https://www.googleapis.com/oauth2/v3/certs`, `id_token_signing_alg_values_supported` `["RS256"]`. The JWKS served two RSA keys with `kty`, `kid`, `use` (`sig`), `alg` (`RS256`), `n`, `e`. The page also says a token received directly from the token endpoint can be trusted; ReviewFlow validates regardless (spec 09 AM-2). | https://developers.google.com/identity/openid-connect/openid-connect (2026-06-15); https://accounts.google.com/.well-known/openid-configuration and https://www.googleapis.com/oauth2/v3/certs (read 2026-10-23) |
| G-14 | Official sample payloads for fixtures | PARTIAL | The reference pages give the JSON shapes (field lists and types) used above, and the web-server page shows sample token and refresh responses. No downloadable full sample responses were found. Fixtures must be built strictly from the documented shapes and marked as such. | pages above |

## Consequences recorded for the spec

How the spec uses these findings (decisions of 2026-10-23, spec 09
AM-1/AM-2):

- **G-4:** still UNVERIFIED. GA-1 (a synchronous exchange) was
  rejected, so the exchange is asynchronous. A code that expires in
  the queue fails safely, and the merchant restarts.
- **G-13:** still UNVERIFIED, and no longer used. OD-10 (b) takes the
  review link from `metadata.newReviewUri` (G-9). The matching change
  to `Google-Reviews.md`, the Data Dictionary and
  FINAL-ARCHITECTURE-REVIEW §7 is RL-1 in the spec, pending sign-off
  of its wording.
- **G-11:** syncing a location needs its account id, so the stored
  `google_location_id` is the full
  `accounts/{accountId}/locations/{locationId}` (OD-12, accepted).
- **G-15:** the ID token is fully validated before any claim is used,
  even though it comes straight from the token endpoint.
- **G-11:** a location that is not verified cannot be synced.
- **G-12:** the documented `Retry-After` handling has no provider
  support; the spec's "stop and resume next tick" behaviour does not
  depend on the header.
- **G-10:** while the OAuth client is in "Testing", refresh tokens
  expire after 7 days, so connections go to `NEEDS_REAUTH` weekly in
  development.
