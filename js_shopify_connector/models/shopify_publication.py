from odoo import fields, models


class ShopifyPublication(models.Model):
    _name = "shopify.publication"
    _description = "Shopify Sales Channel"
    _order = "name"

    name = fields.Char(required=True, readonly=True)
    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade", readonly=True)
    shopify_id = fields.Char(string="Shopify ID", required=True, readonly=True)
    active = fields.Boolean(default=True, readonly=True)
    publish_products = fields.Boolean(
        string="Publish Products Here", help="Products sent from Odoo are published on this sales channel.")

    _shopify_id_uniq = models.Constraint("unique(store_id, shopify_id)", "This sales channel is already loaded.")
