from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_variant_id = fields.Char(string="Shopify Variant ID", readonly=True, copy=False, index="btree_not_null")
    shopify_inventory_item_id = fields.Char(string="Shopify Inventory Item ID", readonly=True, copy=False)
