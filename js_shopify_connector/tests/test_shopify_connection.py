import json
from contextlib import contextmanager
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..services.shopify_client import ShopifyClient, ShopifyError


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload or {}
        self.status_code = status
        self.headers = headers or {}
        self.text = json.dumps(self.payload)

    def json(self):
        return self.payload


def gql(data, available=1000):
    return FakeResponse({"data": data, "extensions": {"cost": {
        "requestedQueryCost": 10, "throttleStatus": {
            "maximumAvailable": 2000, "currentlyAvailable": available, "restoreRate": 100}}}})


@tagged("post_install", "-at_install")
class TestShopifyConnection(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["shopify.store"].search([]).active = False
        cls.store = cls.env["shopify.store"].create({
            "name": "Test Store",
            "shop_domain": "https://Test-Shop.myshopify.com/admin",
            "client_id": "client-id",
            "client_secret": "client-secret",
        })

    @contextmanager
    def mock_shopify(self, *responses):
        calls = []
        queue = list(responses)

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return queue.pop(0)

        with patch.object(ShopifyClient, "_http_post", side_effect=fake_post), \
                patch.object(ShopifyClient, "_sleep") as sleep:
            yield calls, sleep

    def token(self):
        return FakeResponse({"access_token": "shpat_test", "scope": "write_products", "expires_in": 86399})

    def test_domain_normalized(self):
        self.assertEqual(self.store.shop_domain, "test-shop.myshopify.com")
        self.store.shop_domain = "other-shop"
        self.assertEqual(self.store.shop_domain, "other-shop.myshopify.com")
        with self.assertRaises(ValidationError):
            self.store.shop_domain = "shop.example.com"

    def test_one_active_store(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.env["shopify.store"].create({"name": "Second", "shop_domain": "second.myshopify.com"})

    def test_connection_with_app_token(self):
        shop = gql({"shop": {"name": "Saxby & Nash", "myshopifyDomain": "test-shop.myshopify.com",
                             "currencyCode": self.store.company_id.currency_id.name}})
        with self.mock_shopify(self.token(), shop) as (calls, _sleep):
            action = self.store.action_test_connection()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(self.store.state, "connected")
        self.assertEqual(self.store.shop_name, "Saxby & Nash")
        self.assertEqual(calls[0][0], "https://test-shop.myshopify.com/admin/oauth/access_token")
        self.assertEqual(calls[0][1]["data"]["grant_type"], "client_credentials")
        self.assertEqual(calls[1][0], "https://test-shop.myshopify.com/admin/api/2026-10/graphql.json")
        self.assertEqual(calls[1][1]["headers"]["X-Shopify-Access-Token"], "shpat_test")

        # The token is cached until it expires.
        with self.mock_shopify(shop) as (calls, _sleep):
            self.store.action_test_connection()
        self.assertEqual(len(calls), 1)

    def test_connection_error_is_logged(self):
        with self.mock_shopify(FakeResponse({"error": "invalid_client"}, status=400)):
            action = self.store.action_test_connection()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertEqual(self.store.state, "error")
        self.assertIn("refused the app credentials", self.store.last_error)
        log = self.env["shopify.sync.log"].search([("store_id", "=", self.store.id)], limit=1)
        self.assertEqual((log.operation, log.state), ("test_connection", "error"))

    def test_throttled_then_retried(self):
        self.store.write({"auth_method": "access_token", "access_token": "shpat_fixed"})
        throttled = FakeResponse({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                                  "extensions": {"cost": {"requestedQueryCost": 300, "throttleStatus": {
                                      "maximumAvailable": 2000, "currentlyAvailable": 100, "restoreRate": 100}}}})
        with self.mock_shopify(throttled, gql({"shop": {"name": "S"}})) as (calls, sleep):
            data = ShopifyClient(self.store).graphql("query { shop { name } }")
        self.assertEqual(data["shop"]["name"], "S")
        sleep.assert_called_once_with(2.0)
        self.assertEqual(len(calls), 2)

    def test_graphql_and_user_errors(self):
        self.store.write({"auth_method": "access_token", "access_token": "shpat_fixed"})
        with self.mock_shopify(FakeResponse({"errors": [{"message": "Field 'x' doesn't exist"}]})):
            with self.assertRaisesRegex(ShopifyError, "doesn't exist"):
                ShopifyClient(self.store).graphql("query { x }")
        data = {"productSet": {"userErrors": [{"field": ["input", "handle"], "message": "has already been taken"}]}}
        with self.assertRaisesRegex(ShopifyError, "input.handle: has already been taken"):
            ShopifyClient.check_user_errors(data, "productSet")

    def test_fetch_locations_and_channels(self):
        self.store.write({"auth_method": "access_token", "access_token": "shpat_fixed"})
        page = {"hasNextPage": False, "endCursor": None}
        locations = gql({"locations": {"nodes": [
            {"id": "gid://shopify/Location/1", "name": "UK Warehouse", "isActive": True,
             "address": {"countryCode": "GB"}},
            {"id": "gid://shopify/Location/2", "name": "Old 3PL", "isActive": False, "address": None},
        ], "pageInfo": page}})
        with self.mock_shopify(locations):
            self.store.action_fetch_locations()
        uk = self.store.location_ids.filtered(lambda loc: loc.shopify_id == "gid://shopify/Location/1")
        self.assertEqual((uk.name, uk.country_code), ("UK Warehouse", "GB"))

        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        warehouse.shopify_location_id = uk
        self.assertEqual(uk.warehouse_ids, warehouse)

        # Loading again updates in place.
        with self.mock_shopify(locations):
            self.store.action_fetch_locations()
        self.assertEqual(len(self.env["shopify.location"].with_context(active_test=False).search(
            [("store_id", "=", self.store.id)])), 2)

        channels = gql({"publications": {"nodes": [
            {"id": "gid://shopify/Publication/1", "catalog": {"title": "Online Store"}},
            {"id": "gid://shopify/Publication/2", "catalog": {"title": "Google & YouTube"}},
        ], "pageInfo": page}})
        with self.mock_shopify(channels):
            self.store.action_fetch_publications()
        online = self.store.publication_ids.filtered(lambda p: p.name == "Online Store")
        self.assertTrue(online.publish_products, "Online Store is ticked by default.")
        self.assertEqual(len(self.store.publication_ids.filtered("publish_products")), 1)

    def test_default_metafield_map(self):
        maps = self.env["shopify.metafield.map"].search([])
        keys = set(maps.mapped("display_name"))
        self.assertIn("custom.materials", keys)
        self.assertIn("mm-google-shopping.mpn", keys)
        with self.assertRaises(ValidationError):
            maps[0].key = "bad key!"
