import json

from odoo import api, fields, models


class ShopifySyncLog(models.Model):
    _name = "shopify.sync.log"
    _description = "Shopify Sync Log"
    _order = "id desc"
    _rec_name = "operation"

    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade", index=True)
    product_tmpl_id = fields.Many2one("product.template", string="Product", ondelete="set null", index=True)
    operation = fields.Selection([
        ("test_connection", "Test Connection"),
        ("fetch_locations", "Load Locations"),
        ("fetch_publications", "Load Sales Channels"),
        ("publish_product", "Publish Product"),
        ("sync_product", "Update Product"),
        ("upload_media", "Upload Images"),
        ("sync_stock", "Update Stock"),
        ("other", "Other"),
    ], required=True)
    state = fields.Selection([("success", "Success"), ("error", "Error")], required=True)
    message = fields.Text()
    request = fields.Text()
    response = fields.Text()
    duration = fields.Float(string="Duration (s)", digits=(10, 2))

    @api.model
    def _log(self, store, operation, state, message=False, product=False, request=None, response=None, duration=0.0):
        if request is not None and not isinstance(request, str):
            request = json.dumps(request, indent=1, default=str)[:20000]
        return self.sudo().create({
            "store_id": store.id,
            "product_tmpl_id": product and product.id,
            "operation": operation,
            "state": state,
            "message": message,
            "request": request or False,
            "response": (response or "")[:20000] or False,
            "duration": duration,
        })

    @api.autovacuum
    def _gc_old_logs(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=60)
        self.search([("create_date", "<", limit), ("state", "=", "success")]).unlink()
