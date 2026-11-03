from odoo import api, fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_location_id = fields.Many2one(
        "shopify.location", string="Shopify Location", copy=False, ondelete="set null",
        help="Shopify location that receives the available stock of this warehouse.")

    @api.model_create_multi
    def create(self, vals_list):
        warehouses = super().create(vals_list)
        warehouses.shopify_location_id.store_id._shopify_resend_all_stock()
        return warehouses

    def write(self, vals):
        if "shopify_location_id" not in vals:
            return super().write(vals)
        stores = self.shopify_location_id.store_id
        res = super().write(vals)
        (stores | self.shopify_location_id.store_id)._shopify_resend_all_stock()
        return res
