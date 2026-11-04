"""Shipping methods, invoices and payments for imported Shopify orders (Task 3.3).

When Shopify has taken money (a successful Sale or Capture transaction) the order is invoiced in full,
the invoice is posted, and every such transaction becomes one payment in the store's payments journal.
The Shopify transaction ID on the payment makes this safe to run again and again.
"""
from datetime import datetime

from odoo import _, fields, models
from odoo.tools import float_compare, float_is_zero

PAID_KINDS = {"SALE", "CAPTURE"}


class ShopifyShippingMap(models.Model):
    _name = "shopify.shipping.map"
    _description = "Shopify Shipping Method"
    _order = "name"

    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade", index=True)
    name = fields.Char(string="Shopify Shipping Rate", required=True,
                       help="Name of the shipping rate as the customer sees it on Shopify, e.g. Standard.")
    carrier_id = fields.Many2one(
        "delivery.carrier", string="Delivery Method",
        help="Empty: the order uses the store's Shipping Product and no delivery method.")
    order_count = fields.Integer(string="Orders", readonly=True,
                                 help="Imported orders that used this rate.")

    _name_uniq = models.UniqueIndex("(store_id, lower(name))", "This shipping rate is already in the list.")


class AccountPayment(models.Model):
    _inherit = "account.payment"

    shopify_transaction_id = fields.Char(string="Shopify Transaction", readonly=True, copy=False,
                                         index="btree_not_null")


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _compute_qty_to_invoice(self):
        # Shopify has been paid for everything ordered, so the invoice covers the ordered
        # quantities even for products set to invoice on delivery.
        super()._compute_qty_to_invoice()
        for line in self:
            if line.order_id.shopify_order_id and line.state == "sale" and not line.display_type \
                    and line.product_id.type != "combo":
                line.qty_to_invoice = line.product_uom_qty - line.qty_invoiced


class ShopifyStore(models.Model):
    _inherit = "shopify.store"

    shipping_map_ids = fields.One2many("shopify.shipping.map", "store_id", string="Shipping Methods")
    create_invoices = fields.Boolean(
        string="Create Invoices", default=True,
        help="Invoice and register the payment as soon as Shopify has taken the money.")
    invoices_from = fields.Datetime(
        string="Invoice Orders From", default=fields.Datetime.now,
        help="Only orders placed from this time are invoiced automatically. "
             "Use Invoice Paid Orders for older ones.")
    payment_journal_id = fields.Many2one(
        "account.journal", string="Payments Journal", check_company=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
        help="Journal of the payments taken by Shopify. Empty: 'Shopify Payments' is created.")

    def write(self, vals):
        # Switching invoicing on never invoices the orders imported while it was off.
        if vals.get("create_invoices") and "invoices_from" not in vals:
            self.filtered(lambda s: not s.create_invoices).invoices_from = fields.Datetime.now()
        return super().write(vals)

    # ------------------------------------------------------------------
    # Shipping
    # ------------------------------------------------------------------
    def _shopify_shipping_product(self, title, order):
        """Product for a Shopify shipping line; sets the order's delivery method when mapped.
        Unknown rate names are added to the list so a manager can map them."""
        default = self.order_shipping_product_id or self.env.ref(
            "js_shopify_connector.product_shopify_shipping", raise_if_not_found=False)
        if not title:
            return default
        Map = self.env["shopify.shipping.map"].sudo()
        mapping = self.shipping_map_ids.filtered(lambda m: m.name.strip().lower() == title.strip().lower())[:1]
        mapping = mapping.sudo() or Map.create({"store_id": self.id, "name": title.strip()})
        mapping.order_count += 1
        carrier = mapping.carrier_id
        if carrier and carrier.product_id:
            if not order.carrier_id:
                order.carrier_id = carrier
            return carrier.product_id
        return default

    # ------------------------------------------------------------------
    # Invoice and payments
    # ------------------------------------------------------------------
    def _shopify_payment_journal(self):
        self.ensure_one()
        if self.payment_journal_id:
            return self.payment_journal_id
        Journal = self.env["account.journal"].sudo().with_company(self.company_id)
        journal = Journal.search([("company_id", "=", self.company_id.id), ("code", "=", "SHOP"),
                                  ("type", "=", "bank")], limit=1)
        if not journal:
            journal = Journal.create({"name": _("Shopify Payments"), "code": "SHOP", "type": "bank",
                                      "company_id": self.company_id.id})
        self.payment_journal_id = journal
        return journal

    @staticmethod
    def _shopify_paid_transactions(node):
        return [tx for tx in node.get("transactions") or []
                if tx.get("kind") in PAID_KINDS and tx.get("status") == "SUCCESS"
                and float(((tx.get("amountSet") or {}).get("shopMoney") or {}).get("amount") or 0) > 0]

    def _shopify_invoice_and_pay(self, order, node, force=False):
        """Invoice a confirmed order once Shopify has money, and record each new payment.
        Problems are reported on the order; they never stop the import."""
        if not self.create_invoices or order.state != "sale" or order.shopify_import_issue:
            return
        if not force and self.invoices_from and order.date_order < self.invoices_from:
            return  # imported before automatic invoicing: Invoice Paid Orders
        transactions = self._shopify_paid_transactions(node)
        if not transactions:
            return  # e.g. only authorised: invoiced when Shopify captures
        try:
            with self.env.cr.savepoint():
                invoice = self._shopify_order_invoice(order, transactions)
                self._shopify_register_payments(order, invoice, transactions)
        except Exception as error:  # noqa: BLE001 - reported on the order for a person
            self._shopify_add_issue(order, _("The invoice or payment could not be created: %s", error))

    def _shopify_order_invoice(self, order, transactions):
        invoices = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice" and m.state != "cancel")
        if invoices:
            draft = invoices.filtered(lambda m: m.state == "draft")
            draft.action_post()
            return invoices
        paid_on = min(_tx_date(tx) for tx in transactions)
        invoice = order.with_company(order.company_id)._create_invoices(final=True)
        invoice.write({"invoice_date": paid_on})
        invoice.action_post()
        return invoice

    def _shopify_register_payments(self, order, invoices, transactions):
        Payment = self.env["account.payment"].sudo()
        known = set(Payment.search([("shopify_transaction_id", "in", [tx["id"] for tx in transactions])])
                    .mapped("shopify_transaction_id"))
        journal = self._shopify_payment_journal()
        currency = order.currency_id
        for tx in sorted(transactions, key=lambda t: t.get("processedAt") or ""):
            if tx["id"] in known:
                continue
            residual = sum(invoices.mapped("amount_residual"))
            amount = min(_tx_amount(tx), residual)
            if float_is_zero(amount, precision_rounding=currency.rounding):
                continue  # already settled, e.g. paid by hand in Odoo
            wizard = self.env["account.payment.register"].with_company(order.company_id).with_context(
                active_model="account.move", active_ids=invoices.ids).create({
                    "journal_id": journal.id,
                    "amount": amount,
                    "payment_date": _tx_date(tx),
                    "communication": " ".join(filter(None, [order.shopify_order_name, tx.get("gateway")])),
                })
            payments = wizard._create_payments()
            payments.sudo().shopify_transaction_id = tx["id"]
            if float_compare(_tx_amount(tx), amount, precision_rounding=currency.rounding) > 0:
                self._shopify_add_issue(order, _(
                    "Shopify took %(taken)s but only %(recorded)s was still open on the invoice.",
                    taken=f"{_tx_amount(tx):.2f}", recorded=f"{amount:.2f}"))

    # ------------------------------------------------------------------
    # Catch-up
    # ------------------------------------------------------------------
    def action_invoice_paid_orders(self):
        """Invoice paid Shopify orders that were confirmed before automatic invoicing."""
        self.ensure_one()
        orders = self.env["sale.order"].search([
            ("shopify_store_id", "=", self.id), ("state", "=", "sale"), ("shopify_import_issue", "=", False),
            ("shopify_financial_status", "in", ("paid", "partially_paid")),
            ("invoice_status", "=", "to invoice")], limit=50, order="date_order")
        done = 0
        for order in orders:
            node = self._shopify_fetch_order(order.shopify_order_id)
            if node:
                self._shopify_invoice_and_pay(order, node, force=True)
                done += bool(order.invoice_ids.filtered(lambda m: m.state == "posted"))
        message = _("%s Shopify order(s) invoiced.", done)
        if len(orders) == 50:
            message += " " + _("Click again for the next ones.")
        return self._notify(message, "success")


def _tx_amount(tx):
    return float(((tx.get("amountSet") or {}).get("shopMoney") or {}).get("amount") or 0.0)


def _tx_date(tx):
    value = tx.get("processedAt")
    if not value:
        return fields.Date.today()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
