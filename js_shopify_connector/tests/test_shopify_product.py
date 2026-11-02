from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@tagged("post_install", "-at_install")
class TestShopifyProduct(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env["product.template"]
        cls.uk = cls.env.ref("js_product_sku.territory_uk")
        cls.public_categ = cls.env["product.public.category"].create(
            {"name": "Dog Beds", "shopify_taxonomy_id": "ap-2-3-2"})
        cls.tag = cls.env["product.tag"].create({"name": "pets"})

    def _ready_product(self, **vals):
        template = self.Template.create({
            "name": "Cane Dog Bed",
            "type": "consu",
            "is_storable": True,
            "territory_id": self.uk.id,
            "shopify_enabled": True,
            "list_price": 195.0,
            "weight": 8.5,
            "barcode": "5060000000017",
            "default_code": "SN-PET-CDB",
            "description_ecommerce": "<p>Hand-woven cane on a solid oak frame.</p>",
            "product_tag_ids": [(6, 0, self.tag.ids)],
            "public_categ_ids": [(6, 0, self.public_categ.ids)],
            "image_1920": PNG_1PX,
            "hs_code": "9403.89",
            "country_of_origin": self.env.ref("base.cn").id,
            **vals,
        })
        return template

    def test_handle_from_name(self):
        template = self.Template.create({"name": "Cane Dog Bed"})
        self.assertEqual(template.seo_name, "cane-dog-bed")
        template.name = "Cane Dog Bed Large"
        self.assertEqual(template.seo_name, "cane-dog-bed-large", "A default handle follows the name.")

    def test_custom_handle_kept(self):
        template = self.Template.create({"name": "Cane Dog Bed", "seo_name": "My Dog Bed!"})
        self.assertEqual(template.seo_name, "my-dog-bed", "The handle is cleaned into a slug.")
        template.name = "Other Name"
        self.assertEqual(template.seo_name, "my-dog-bed", "A handle typed by hand does not follow the name.")

    def test_copy_gets_own_handle(self):
        template = self.Template.create({"name": "Cane Dog Bed"})
        self.assertEqual(template.copy().seo_name, "cane-dog-bed-copy")

    def test_handle_locked_once_published(self):
        template = self._ready_product()
        template.write({"shopify_product_id": "gid://shopify/Product/1"})
        with self.assertRaises(ValidationError):
            template.seo_name = "new-handle"
        template.name = "Renamed"
        self.assertEqual(template.seo_name, "cane-dog-bed", "A published handle does not follow the name.")

    def test_handle_unique_on_shopify(self):
        self._ready_product()
        with self.assertRaises(ValidationError):
            self._ready_product(default_code="SN-PET-CDB-2", barcode="5060000000024")

    def test_only_uk_territory(self):
        other = self.env["product.territory"].create({"code": "99", "name": "Elsewhere"})
        with self.assertRaises(ValidationError):
            self.Template.create({"name": "Lamp", "territory_id": other.id, "shopify_enabled": True})

    def test_out_of_stock_default_deny(self):
        self.assertFalse(self.Template.create({"name": "Lamp"}).allow_out_of_stock_order)

    def test_publish_issues(self):
        self.assertFalse(self._ready_product()._get_shopify_publish_issues())

        template = self.Template.create({"name": "Lamp", "type": "consu"})
        issues = template._get_shopify_publish_issues()
        self.assertTrue(any("Sell on Shopify is off" in issue for issue in issues))
        self.assertTrue(any("HS code" in issue for issue in issues))
        self.assertTrue(any("without an SKU" in issue for issue in issues))
        with self.assertRaises(UserError):
            template.action_shopify_check()

    def test_category_without_taxonomy(self):
        categ = self.env["product.public.category"].create({"name": "Lamps"})
        template = self._ready_product(public_categ_ids=[(6, 0, categ.ids)])
        self.assertIn("eCommerce category Lamps has no Shopify taxonomy ID.", template._get_shopify_publish_issues())
