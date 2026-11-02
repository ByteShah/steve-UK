from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_location_id = fields.Many2one(
        "shopify.location", string="Shopify Location", copy=False, ondelete="set null",
        help="Shopify location that receives the available stock of this warehouse.")
