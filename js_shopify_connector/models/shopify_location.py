from odoo import fields, models


class ShopifyLocation(models.Model):
    _name = "shopify.location"
    _description = "Shopify Location"
    _order = "name"

    name = fields.Char(required=True, readonly=True)
    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade", readonly=True)
    shopify_id = fields.Char(string="Shopify ID", required=True, readonly=True)
    country_code = fields.Char(readonly=True)
    active = fields.Boolean(default=True, readonly=True, help="Inactive in Shopify, or deleted there.")
    warehouse_ids = fields.One2many(
        "stock.warehouse", "shopify_location_id", string="Warehouses",
        help="Warehouses whose available stock is sent to this location.")

    _shopify_id_uniq = models.Constraint("unique(store_id, shopify_id)", "This Shopify location is already loaded.")
