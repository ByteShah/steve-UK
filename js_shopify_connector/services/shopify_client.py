"""Shopify Admin GraphQL API client: authentication, rate limits and retries."""
import logging
import time
from datetime import timedelta

import requests

from odoo import fields

_logger = logging.getLogger(__name__)

TIMEOUT = 30
MAX_ATTEMPTS = 5
TOKEN_MARGIN = timedelta(minutes=5)


class ShopifyError(Exception):
    """Shopify refused the request or could not be reached."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class ShopifyClient:
    """One client per `shopify.store` record."""

    def __init__(self, store):
        store.ensure_one()
        self.store = store.sudo()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    @property
    def base_url(self):
        return f"https://{self.store.shop_domain}"

    def _get_access_token(self, force_refresh=False):
        store = self.store
        if store.auth_method == "access_token":
            if not store.access_token:
                raise ShopifyError("No Admin API access token is set on the store.")
            return store.access_token
        now = fields.Datetime.now()
        if (not force_refresh and store.access_token and store.access_token_expiry
                and store.access_token_expiry - TOKEN_MARGIN > now):
            return store.access_token
        if not (store.client_id and store.client_secret):
            raise ShopifyError("The app client ID and client secret are needed to connect.")
        response = self._http_post(
            f"{self.base_url}/admin/oauth/access_token",
            data={"grant_type": "client_credentials",
                  "client_id": store.client_id, "client_secret": store.client_secret},
        )
        if response.status_code != 200:
            raise ShopifyError(
                f"Shopify refused the app credentials (HTTP {response.status_code}): {response.text[:500]}",
                response=response.text)
        payload = response.json()
        store.write({
            "access_token": payload["access_token"],
            "access_token_expiry": now + timedelta(seconds=int(payload.get("expires_in") or 86399)),
        })
        return payload["access_token"]

    # ------------------------------------------------------------------
    # GraphQL
    # ------------------------------------------------------------------
    def graphql(self, query, variables=None):
        """Run a query or mutation and return its `data`. Waits on throttling, retries
        network and server errors, refreshes an expired app token once."""
        url = f"{self.base_url}/admin/api/{self.store.api_version}/graphql.json"
        body = {"query": query, "variables": variables or {}}
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"X-Shopify-Access-Token": self._get_access_token(), "Content-Type": "application/json"}
            try:
                response = self._http_post(url, json=body, headers=headers)
            except requests.RequestException as error:
                if attempt == MAX_ATTEMPTS:
                    raise ShopifyError(f"Shopify could not be reached: {error}") from error
                self._sleep(2 ** attempt)
                continue

            if response.status_code == 401 and self.store.auth_method == "client_credentials" and not refreshed:
                self._get_access_token(force_refresh=True)
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise ShopifyError(f"Shopify is not answering (HTTP {response.status_code}).",
                                       response=response.text)
                self._sleep(float(response.headers.get("Retry-After") or 2 ** attempt))
                continue
            if response.status_code != 200:
                raise ShopifyError(f"Shopify refused the request (HTTP {response.status_code}): "
                                   f"{response.text[:500]}", response=response.text)

            result = response.json()
            errors = result.get("errors")
            if errors and self._is_throttled(errors):
                if attempt == MAX_ATTEMPTS:
                    raise ShopifyError("Shopify kept throttling the request.", response=response.text)
                self._sleep(self._throttle_wait(result))
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors), response=response.text)
            self._slow_down_if_needed(result)
            return result.get("data") or {}
        raise ShopifyError("Shopify request failed.")

    def upload_staged(self, target, filename, content, mimetype):
        """Send a file to a target returned by stagedUploadsCreate (POST form upload)."""
        data = {p["name"]: p["value"] for p in target.get("parameters") or []}
        try:
            response = self._http_post(target["url"], data=data, files={"file": (filename, content, mimetype)})
        except requests.RequestException as error:
            raise ShopifyError(f"The image {filename} could not be uploaded: {error}") from error
        if response.status_code not in (200, 201, 204):
            raise ShopifyError(f"The image {filename} could not be uploaded (HTTP {response.status_code}).",
                               response=response.text)
        return target["resourceUrl"]

    @staticmethod
    def check_user_errors(data, mutation):
        """Raise when a mutation answered with userErrors."""
        user_errors = (data.get(mutation) or {}).get("userErrors") or []
        if user_errors:
            raise ShopifyError("; ".join(
                f"{'.'.join(map(str, e.get('field') or []))}: {e.get('message')}".lstrip(": ")
                for e in user_errors))
        return data.get(mutation) or {}

    # ------------------------------------------------------------------
    # Rate limits (cost-based leaky bucket)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_throttled(errors):
        return any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)

    @staticmethod
    def _throttle_status(result):
        cost = (result.get("extensions") or {}).get("cost") or {}
        return cost.get("requestedQueryCost") or 0, cost.get("throttleStatus") or {}

    def _throttle_wait(self, result):
        requested, status = self._throttle_status(result)
        available = status.get("currentlyAvailable") or 0
        restore = status.get("restoreRate") or 50
        return max(1.0, (requested - available) / restore)

    def _slow_down_if_needed(self, result):
        # Keep a reserve in the bucket so the next call is not throttled.
        _requested, status = self._throttle_status(result)
        if status and status.get("currentlyAvailable", 1000) < status.get("maximumAvailable", 1000) * 0.1:
            self._sleep(1.0)

    # ------------------------------------------------------------------
    # Low level, patched in tests
    # ------------------------------------------------------------------
    @staticmethod
    def _http_post(url, **kwargs):
        return requests.post(url, timeout=TIMEOUT, **kwargs)

    @staticmethod
    def _sleep(seconds):
        _logger.info("Shopify: waiting %.1fs before the next call", seconds)
        time.sleep(seconds)
