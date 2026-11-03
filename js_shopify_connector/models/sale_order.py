from odoo import _, api, fields, models
from odoo.exceptions import UserError

FINANCIAL_STATUSES = [
    ("pending", "Pending"),
    ("authorized", "Authorised"),
    ("partially_paid", "Partially Paid"),
    ("paid", "Paid"),
    ("partially_refunded", "Partially Refunded"),
    ("refunded", "Refunded"),
    ("voided", "Voided"),
    ("expired", "Expired"),
]

FULFILLMENT_STATUSES = [
    ("unfulfilled", "Unfulfilled"),
    ("partially_fulfilled", "Partially Fulfilled"),
    ("fulfilled", "Fulfilled"),
    ("in_progress", "In Progress"),
    ("on_hold", "On Hold"),
    ("open", "Open"),
    ("pending_fulfillment", "Pending Fulfilment"),
    ("scheduled", "Scheduled"),
    ("request_declined", "Request Declined"),
    ("restocked", "Restocked"),
]


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shopify_store_id = fields.Many2one("shopify.store", string="Shopify Store", readonly=True, copy=False, index=True)
    shopify_order_id = fields.Char(string="Shopify Order ID", readonly=True, copy=False)
    shopify_order_name = fields.Char(string="Shopify Order", readonly=True, copy=False, index="trigram",
                                     help="Order number shown in Shopify, e.g. #1001.")
    shopify_financial_status = fields.Selection(
        FINANCIAL_STATUSES, string="Shopify Payment", readonly=True, copy=False)
    shopify_fulfillment_status = fields.Selection(
        FULFILLMENT_STATUSES, string="Shopify Fulfilment", readonly=True, copy=False)
    shopify_total = fields.Monetary(string="Shopify Total", readonly=True, copy=False,
                                    help="Order total charged by Shopify, compared with the Odoo total on import.")
    shopify_updated_at = fields.Datetime(string="Shopify Last Update", readonly=True, copy=False)
    shopify_import_issue = fields.Text(
        string="Needs Attention", readonly=True, copy=False,
        help="Why this Shopify order was not confirmed automatically.")

    _shopify_order_uniq = models.UniqueIndex(
        "(shopify_store_id, shopify_order_id) WHERE shopify_order_id IS NOT NULL",
        "This Shopify order has already been imported.")

    @api.ondelete(at_uninstall=False)
    def _unlink_except_shopify_order(self):
        # Deleting would let the next run import the order again; cancel it instead.
        if any(order.shopify_order_id and order.state != "cancel" for order in self):
            raise UserError(_("Shopify orders cannot be deleted. Cancel the order instead."))

    def action_confirm(self):
        # Confirming sets the order date to now; Shopify orders keep the date the customer ordered.
        ordered_on = {order.id: order.date_order for order in self if order.shopify_order_id}
        res = super().action_confirm()
        for order in self.filtered(lambda o: o.id in ordered_on):
            order.date_order = ordered_on[order.id]
        return res

    def action_shopify_open_order(self):
        self.ensure_one()
        shop = self.shopify_store_id.shop_domain.removesuffix(".myshopify.com")
        order_number = self.shopify_order_id.rsplit("/", 1)[-1]
        return {"type": "ir.actions.act_url", "target": "new",
                "url": f"https://admin.shopify.com/store/{shop}/orders/{order_number}"}

    def action_shopify_reimport(self):
        """Rebuild a quotation from Shopify, e.g. after fixing an unknown SKU."""
        self.ensure_one()
        if not self.shopify_order_id:
            raise UserError(_("This order does not come from Shopify."))
        if self.state not in ("draft", "sent"):
            raise UserError(_("Only quotations can be imported again. This order is already confirmed."))
        order = self.shopify_store_id._shopify_import_order_by_id(self.shopify_order_id)
        if order and not order.shopify_import_issue:
            message, kind = _("Imported again from Shopify; everything matched."), "success"
        else:
            message, kind = _("Imported again from Shopify; it still needs attention."), "warning"
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": kind, "message": message,
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    shopify_line_id = fields.Char(string="Shopify Line ID", readonly=True, copy=False)
