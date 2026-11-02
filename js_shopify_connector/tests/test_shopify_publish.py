import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..services.product_payload import ProductPayloadBuilder
from ..services.rich_text import html_to_rich_text
from ..services.shopify_client import ShopifyClient
from .test_shopify_connection import FakeResponse, gql
from .test_shopify_product import PNG_1PX

PRODUCT_GID = "gid://shopify/Product/100"


@tagged("post_install", "-at_install")
class TestShopifyPublish(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["shopify.store"].search([]).active = False
        cls.store = env["shopify.store"].create({
            "name": "Test Store", "shop_domain": "test-shop.myshopify.com",
            "auth_method": "access_token", "access_token": "shpat_fixed", "vendor": "Saxby & Nash",
        })
        cls.location = env["shopify.location"].create({
            "store_id": cls.store.id, "shopify_id": "gid://shopify/Location/1", "name": "UK Warehouse"})
        cls.warehouse = env["stock.warehouse"].search([("company_id", "=", env.company.id)], limit=1)
        cls.warehouse.shopify_location_id = cls.location
        cls.online_store = env["shopify.publication"].create({
            "store_id": cls.store.id, "shopify_id": "gid://shopify/Publication/1", "name": "Online Store",
            "publish_products": True})

        cls.vat = env["account.tax"].create({
            "name": "VAT 20% (test)", "amount": 20, "amount_type": "percent", "type_tax_use": "sale",
            "price_include_override": "tax_excluded"})
        cls.colour = env["product.attribute"].create({"name": "Colour", "create_variant": "always", "value_ids": [
            (0, 0, {"name": "Natural", "sequence": 1}), (0, 0, {"name": "Black", "sequence": 2})]})
        partner = env["res.partner"].create({
            "name": "Saxby & Nash Ltd", "street": "1 High Street", "city": "London", "zip": "N1 1AA",
            "country_id": env.ref("base.uk").id, "email": "safety@example.com"})

        cls.template = env["product.template"].create({
            "name": "Cane Dog Bed",
            "type": "consu",
            "is_storable": True,
            "territory_id": env.ref("js_product_sku.territory_uk").id,
            "shopify_enabled": True,
            "list_price": 162.5,
            "taxes_id": [(6, 0, cls.vat.ids)],
            "categ_id": env["product.category"].create({"name": "Dog Beds"}).id,
            "description_ecommerce": "<p>Hand-woven cane on a solid oak frame.</p>",
            "product_tag_ids": [(0, 0, {"name": "pets"}), (0, 0, {"name": "new-in"})],
            "public_categ_ids": [(0, 0, {"name": "Dog Beds", "shopify_taxonomy_id": "ap-2-3-2"})],
            "image_1920": PNG_1PX,
            "hs_code": "9403.89",
            "country_of_origin": env.ref("base.cn").id,
            "materials": "Rattan cane, solid oak",
            "width": 90, "depth": 70, "height": 22,
            "care_instructions": "<p>Wipe with a <b>dry</b> cloth.</p>",
            "gpsr_manufacturer_id": partner.id,
            "attribute_line_ids": [(0, 0, {"attribute_id": cls.colour.id, "value_ids": [(6, 0, cls.colour.value_ids.ids)]})],
        })
        for code, variant in zip(("SN-CDB-NAT", "SN-CDB-BLK"), cls.template.product_variant_ids.sorted("id")):
            variant.write({"default_code": code, "barcode": code + "-EAN", "weight": 8.5})
        cls.natural = cls.template.product_variant_ids.filtered(lambda v: v.default_code == "SN-CDB-NAT")
        env["stock.quant"]._update_available_quantity(cls.natural, cls.warehouse.lot_stock_id, 24)

    def run_publish(self, *responses):
        queue, calls = list(responses), []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return queue.pop(0)

        with patch.object(ShopifyClient, "_http_post", side_effect=fake_post), patch.object(ShopifyClient, "_sleep"):
            action = self.template.action_shopify_publish()
        self.assertFalse(queue, "Every expected Shopify call was made.")
        return action, calls

    @staticmethod
    def body(call):
        return call[1]["json"]

    def product_set_response(self):
        return gql({"productSet": {"product": {"id": PRODUCT_GID, "handle": "cane-dog-bed", "variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/1", "sku": "SN-CDB-NAT", "inventoryItem": {"id": "gid://shopify/InventoryItem/1"}},
            {"id": "gid://shopify/ProductVariant/2", "sku": "SN-CDB-BLK", "inventoryItem": {"id": "gid://shopify/InventoryItem/2"}},
        ]}}, "userErrors": []}})

    def first_publish(self):
        staged = gql({"stagedUploadsCreate": {"stagedTargets": [
            {"url": "https://uploads.example/upload", "resourceUrl": "https://uploads.example/r/1",
             "parameters": [{"name": "key", "value": "abc"}]}], "userErrors": []}})
        files = gql({"fileCreate": {"files": [{"id": "gid://shopify/MediaImage/9", "fileStatus": "UPLOADED"}],
                                    "userErrors": []}})
        published = gql({"publishablePublish": {"userErrors": []}})
        return self.run_publish(staged, FakeResponse(status=204), files, self.product_set_response(), published)

    def test_first_publish(self):
        action, calls = self.first_publish()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(calls[1][0], "https://uploads.example/upload", "The image goes to the staged target.")
        self.assertEqual(calls[1][1]["data"], {"key": "abc"})

        variables = self.body(calls[3])["variables"]
        self.assertEqual(variables["identifier"], {"handle": "cane-dog-bed"}, "A first publish matches on handle.")
        product = variables["input"]
        self.assertEqual(product["title"], "Cane Dog Bed")
        self.assertEqual(product["vendor"], "Saxby & Nash")
        self.assertEqual(product["productType"], "Dog Beds")
        self.assertEqual(product["category"], "gid://shopify/TaxonomyCategory/ap-2-3-2")
        self.assertEqual(product["status"], "DRAFT")
        self.assertEqual(sorted(product["tags"]), ["new-in", "pets"])
        self.assertEqual(product["seo"]["title"], "Cane Dog Bed")
        self.assertEqual(product["files"], [{"id": "gid://shopify/MediaImage/9", "alt": "Cane Dog Bed",
                                             "contentType": "IMAGE"}])
        self.assertEqual(product["productOptions"], [{"name": "Colour", "position": 1,
                                                      "values": [{"name": "Natural"}, {"name": "Black"}]}])

        natural = next(v for v in product["variants"] if v["sku"] == "SN-CDB-NAT")
        self.assertEqual(natural["optionValues"], [{"optionName": "Colour", "name": "Natural"}])
        self.assertEqual(natural["price"], "195.0", "Shopify gets the UK price including VAT.")
        self.assertTrue(natural["taxable"])
        self.assertEqual(natural["inventoryPolicy"], "DENY")
        self.assertEqual(natural["inventoryItem"]["harmonizedSystemCode"], "940389")
        self.assertEqual(natural["inventoryItem"]["countryCodeOfOrigin"], "CN")
        self.assertEqual(natural["inventoryItem"]["measurement"]["weight"], {"value": 8.5, "unit": "KILOGRAMS"})
        self.assertEqual(natural["inventoryQuantities"], [
            {"locationId": "gid://shopify/Location/1", "name": "available", "quantity": 24}])
        self.assertNotIn("id", natural)

        metafields = {(m["namespace"], m["key"]): m for m in product["metafields"]}
        self.assertEqual(metafields[("custom", "materials")]["value"], "Rattan cane, solid oak")
        self.assertEqual(json.loads(metafields[("custom", "width")]["value"]), {"value": 90.0, "unit": "cm"})
        self.assertIn("1 High Street", metafields[("custom", "gpsr_manufacturer")]["value"])
        self.assertIn("safety@example.com", metafields[("custom", "gpsr_manufacturer")]["value"])
        self.assertEqual(json.loads(metafields[("custom", "care_instructions")]["value"])["type"], "root")
        self.assertNotIn(("mm-google-shopping", "age_group"), metafields, "Google fields need Google Shopping on.")

        publish = self.body(calls[4])["variables"]
        self.assertEqual(publish, {"id": PRODUCT_GID, "input": [{"publicationId": "gid://shopify/Publication/1"}]})

        self.assertEqual(self.template.shopify_product_id, PRODUCT_GID)
        self.assertEqual(self.template.shopify_sync_state, "synced")
        self.assertTrue(self.template.shopify_last_synced)
        self.assertEqual(self.natural.shopify_variant_id, "gid://shopify/ProductVariant/1")
        self.assertEqual(self.natural.shopify_inventory_item_id, "gid://shopify/InventoryItem/1")

    def test_update_reuses_ids_and_images(self):
        self.first_publish()
        self.template.materials = False
        deleted = gql({"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}})
        stock = gql({"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "x"}, "userErrors": []}})
        published = gql({"publishablePublish": {"userErrors": []}})
        _action, calls = self.run_publish(self.product_set_response(), deleted, stock, published)

        product_set = self.body(calls[0])["variables"]
        self.assertEqual(product_set["identifier"], {"id": PRODUCT_GID})
        self.assertEqual({v["id"] for v in product_set["input"]["variants"]},
                         {"gid://shopify/ProductVariant/1", "gid://shopify/ProductVariant/2"})
        self.assertTrue(all("inventoryQuantities" not in v for v in product_set["input"]["variants"]))
        self.assertEqual(product_set["input"]["files"][0]["id"], "gid://shopify/MediaImage/9",
                         "An unchanged image is not uploaded again.")

        to_delete = self.body(calls[1])["variables"]["metafields"]
        self.assertIn({"ownerId": PRODUCT_GID, "namespace": "custom", "key": "materials"}, to_delete)

        quantities = self.body(calls[2])["variables"]["input"]["quantities"]
        natural = next(q for q in quantities if q["inventoryItemId"] == "gid://shopify/InventoryItem/1")
        self.assertEqual((natural["quantity"], natural["changeFromQuantity"]), (24, None))

    def test_user_errors_stored_on_product(self):
        self.template.image_1920 = False
        rejected = gql({"productSet": {"product": None, "userErrors": [
            {"field": ["input", "handle"], "message": "has already been taken", "code": "TAKEN"}]}})
        with patch.object(type(self.template), "_get_shopify_publish_issues", return_value=[]):
            action, _calls = self.run_publish(rejected)
        self.assertEqual(action["params"]["type"], "danger")
        self.assertEqual(self.template.shopify_sync_state, "error")
        self.assertIn("has already been taken", self.template.shopify_sync_error)
        self.assertFalse(self.template.shopify_product_id)
        log = self.env["shopify.sync.log"].search([("product_tmpl_id", "=", self.template.id)], limit=1)
        self.assertEqual((log.operation, log.state), ("publish_product", "error"))

    def test_issues_block_publish(self):
        self.template.hs_code = False
        with self.assertRaisesRegex(UserError, "HS code"):
            self.template.action_shopify_publish()

    def test_single_variant_product(self):
        template = self.env["product.template"].create({"name": "Linen Pet Blanket", "default_code": "SN-LPB"})
        payload = ProductPayloadBuilder(template, self.store).build()
        self.assertEqual(payload["productOptions"], [{"name": "Title", "position": 1,
                                                      "values": [{"name": "Default Title"}]}])
        self.assertEqual(payload["variants"][0]["optionValues"], [{"optionName": "Title", "name": "Default Title"}])
        self.assertEqual(payload["variants"][0]["sku"], "SN-LPB")

    def test_google_metafields(self):
        self.template.write({"enable_google_shopping": True, "google_age_group": "adult", "google_gender": "unisex"})
        metafields = {(m["namespace"], m["key"]): m["value"]
                      for m in ProductPayloadBuilder(self.template, self.store).metafields()[0]}
        self.assertEqual(metafields[("mm-google-shopping", "age_group")], "adult")
        self.assertEqual(metafields[("mm-google-shopping", "condition")], "new")

    def test_rich_text(self):
        value = json.loads(html_to_rich_text("<p>Wash at <b>30°</b>.</p><ul><li>Dry flat</li></ul>"))
        self.assertEqual(value["children"][0]["children"], [
            {"type": "text", "value": "Wash at "}, {"type": "text", "value": "30°", "bold": True},
            {"type": "text", "value": "."}])
        self.assertEqual(value["children"][1]["listType"], "unordered")
        self.assertFalse(html_to_rich_text("<p><br></p>"))
