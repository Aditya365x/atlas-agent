# Northwind Freight API reference (v3)

The Freight API lets retailers create shipments, get quotes, track parcels and download invoices. Base URL: https://api.northwind.example/v3. All requests and responses are JSON.

## Authentication

Authenticate every request with an API key in the `Authorization: Bearer <key>` header. Keys are created in the dashboard under Settings, Developers. Test keys start with `nw_test_` and live keys start with `nw_live_`. Test keys never create real pickups. Rotate keys at least every 90 days; old keys keep working for 24 hours after rotation so you can deploy the new one.

## Rate limits

Each API key may send 600 requests per minute. The quotes endpoint has a stricter limit of 120 requests per minute because it calls carrier pricing engines. When you exceed a limit the API returns HTTP 429 with error code E-1429 and a `Retry-After` header in seconds. Use exponential backoff with jitter.

## Idempotency

POST requests accept an `Idempotency-Key` header. If the same key is sent again within 24 hours, the API returns the original response instead of creating a duplicate shipment. Use a UUID per logical operation.

## Create a shipment

`POST /shipments` with pickup address, drop address, parcel dimensions in centimetres, weight in kilograms and the service level (`same_day`, `next_day` or `economy`). The response contains the shipment ID in the format NW-SHP-XXXXXXX and a label URL. Same-day shipments must be created before 13:00 local time for pickup the same afternoon.

## Quotes

`POST /quotes` returns prices for every service level available on a route. Quotes are valid for 15 minutes. Volumetric weight is calculated as length x width x height / 5000; you are charged for the higher of actual and volumetric weight.

## Tracking and webhooks

`GET /shipments/{id}/tracking` returns the event history. Instead of polling, register a webhook to receive `shipment.picked_up`, `shipment.in_transit`, `shipment.out_for_delivery`, `shipment.delivered` and `shipment.exception` events. Webhooks are signed with HMAC-SHA256 in the `NW-Signature` header; verify the signature with your webhook secret before trusting the payload. Failed webhook deliveries are retried 8 times over 24 hours with exponential backoff.

## Cancellations

A shipment can be cancelled free of charge until a driver is assigned. After a driver is assigned the cancellation fee is 30 percent of the shipment price, and after pickup a shipment cannot be cancelled, only returned.

## Error codes

E-1001: invalid or missing API key. Check the Authorization header.

E-1002: the API key is valid but has been revoked or has expired.

E-1429: rate limit exceeded. Honour the Retry-After header.

E-2001: address could not be geocoded. Send a full postal address with PIN code.

E-2002: route not serviceable. The drop PIN code is outside our coverage area for the chosen service level.

E-2003: parcel exceeds limits. The maximum weight is 50 kg per parcel and the longest side may not exceed 150 cm.

E-3001: payment method declined. Update the card or switch to invoicing in the dashboard.

E-4031: the shipment cannot be modified because a driver is already assigned. Cancel and recreate it instead.

E-4040: shipment not found. The ID is wrong or belongs to another account.

E-5000: internal error. Safe to retry with the same Idempotency-Key.

## Invoices

Invoices are generated on the 1st of each month for the previous month and can be downloaded as PDF or CSV from `GET /invoices`. Payment terms are 15 days for accounts on invoicing. Late payments accrue interest at 1.5 percent per month.

## Sandbox environment

The sandbox at https://sandbox.api.northwind.example/v3 mirrors production. Use PIN code 999999 to simulate a route that is not serviceable, and weight 49.99 kg to simulate a carrier exception.
