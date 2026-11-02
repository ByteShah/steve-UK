from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .test_shopify_connection import gql
from .test_shopify_publish import PRODUCT_GID, ShopifyPublishCase

OK_STOCK = {"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "x"}, "userErrors": []}}


@tagged("post_install", "-at_install")
class TestShopifySync(ShopifyPublishCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ProductTemplate = type(cls.env["product.template"])

    def setUp(self):
        super().setUp()
        self.first_publish()
        self.assertEqual(self.template.shopify_sync_state, "synced")

    def update_responses(self):
        return (self.product_set_response(),
                gql({"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}}),
                gql(OK_STOCK),
                gql({"publishablePublish": {"userErrors": []}}))

    # ------------------------------------------------------------------
    # Change tracking
    # ------------------------------------------------------------------
    def test_changes_mark_to_sync(self):
        changes = [
            lambda: self.template.write({"name": "Cane Dog Bed XL"}),
            lambda: self.template.write({"materials": "Oak"}),            # metafield field
            lambda: self.natural.write({"barcode": "5060000000999"}),     # variant field
            lambda: self.env["product.image"].create({"name": "Side", "product_tmpl_id": self.template.id}),
            lambda: self.template.categ_id.write({"name": "Beds"}),         # product type
            lambda: self.template.product_tag_ids[:1].write({"name": "pet"}),
            lambda: self.template.public_categ_ids.write({"shopify_taxonomy_id": "ap-2-3-3"}),
            lambda: self.template.write({"active": False}),
        ]
        for change in changes:
            self.template.sudo().shopify_sync_state = "synced"
            change()
            self.assertEqual(self.template.shopify_sync_state, "to_sync")

    def test_untracked_change_keeps_synced(self):
        self.template.write({"description_purchase": "Only for buyers"})
        self.assertEqual(self.template.shopify_sync_state, "synced")

    def test_unpublished_product_not_marked(self):
        other = self.env["product.template"].create({"name": "Lamp"})
        other.name = "Floor Lamp"
        self.assertEqual(other.shopify_sync_state, "not_published")

    def test_published_product_cannot_be_deleted(self):
        with self.assertRaisesRegex(UserError, "Archive it instead"):
            self.template.unlink()

    # ------------------------------------------------------------------
    # Product cron
    # ------------------------------------------------------------------
    def test_cron_updates_changed_products(self):
        self.template.name = "Cane Dog Bed XL"
        (done, failed), calls = self.run_shopify(self.store._sync_changed_products, *self.update_responses())
        self.assertEqual((done, failed), (1, 0))
        self.assertEqual(self.body(calls[0])["variables"]["input"]["title"], "Cane Dog Bed XL")
        self.assertEqual(self.template.shopify_sync_state, "synced")

    def test_archive_in_odoo_archives_in_shopify(self):
        self.template.active = False
        archived = gql({"productUpdate": {"product": {"id": PRODUCT_GID, "status": "ARCHIVED"}, "userErrors": []}})
        (done, _failed), calls = self.run_shopify(self.store._sync_changed_products, archived)
        self.assertEqual(done, 1)
        self.assertEqual(self.body(calls[0])["variables"],
                         {"product": {"id": PRODUCT_GID, "status": "ARCHIVED"}})
        self.assertEqual(self.template.shopify_sync_state, "synced")

    def test_switch_off_archives_in_shopify(self):
        self.template.shopify_enabled = False
        archived = gql({"productUpdate": {"product": {"id": PRODUCT_GID, "status": "ARCHIVED"}, "userErrors": []}})
        _result, calls = self.run_shopify(self.store._sync_changed_products, archived)
        self.assertIn("productUpdate", self.body(calls[0])["query"])

    def test_unexpected_error_does_not_stop_others(self):
        self.template.name = "Cane Dog Bed XL"
        with patch.object(self.ProductTemplate, "_shopify_publish", side_effect=ValueError("boom")), \
                mute_logger("odoo.addons.js_shopify_connector.models.shopify_store"):
            done, failed = self.store._sync_changed_products()
        self.assertEqual((done, failed), (0, 1))
        self.assertEqual(self.template.shopify_sync_state, "error")
        self.assertIn("boom", self.template.shopify_sync_error)

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    def test_stock_sync_sends_changed_variants(self):
        self.store.stock_synced_until = fields.Datetime.now() - timedelta(minutes=5)
        self.env["stock.quant"]._update_available_quantity(self.natural, self.warehouse.lot_stock_id, 6)
        count, calls = self.run_shopify(self.store._sync_stock, gql(OK_STOCK))
        self.assertEqual(count, 1, "Only the variant whose stock moved is sent.")
        quantities = self.body(calls[0])["variables"]["input"]["quantities"]
        self.assertEqual([(q["inventoryItemId"], q["quantity"]) for q in quantities],
                         [("gid://shopify/InventoryItem/1", 30)])
        self.assertTrue(self.store.stock_synced_until)

    def test_stock_sync_error_keeps_position(self):
        since = fields.Datetime.now() - timedelta(minutes=5)
        self.store.stock_synced_until = since
        self.env["stock.quant"]._update_available_quantity(self.natural, self.warehouse.lot_stock_id, 6)
        refused = gql({"inventorySetQuantities": {"userErrors": [{"field": ["input"], "message": "Invalid"}]}})
        result, _calls = self.run_shopify(self.store._sync_stock, refused)
        self.assertFalse(result)
        self.assertEqual(self.store.stock_synced_until, since, "The same changes are sent again next run.")

    def test_new_location_gets_activated(self):
        not_stocked = gql({"inventorySetQuantities": {"userErrors": [
            {"field": ["input"], "message": "The specified inventory item is not stocked at the location."}]}})
        activated = gql({"inventoryActivate": {"inventoryLevel": {"id": "l"}, "userErrors": []}})
        responses = [not_stocked, activated, activated, gql(OK_STOCK)]
        count, calls = self.run_shopify(lambda: self.store._sync_stock(full=True), *responses)
        self.assertEqual(count, 2)
        self.assertIn("inventoryActivate", self.body(calls[1])["query"])
