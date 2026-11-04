"""Cancellations, refunds and returns made in Shopify (Task 3.5).

Each Shopify refund of a confirmed order is processed once and kept as a Shopify Refund on the order:
items not shipped yet are taken off the order (its delivery shrinks), restocked shipped items get a
return receipt, and the money paid back becomes a posted credit note with a refund payment in the
store's payments journal. An order cancelled in Shopify is then cancelled in Odoo when nothing has
shipped; otherwise its open deliveries are cancelled and a person is told.
"""
from collections import defaultdict

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

from ..services.shopify_client import ShopifyError
from .shopify_order_payment import _tx_amount, _tx_date

REFUNDS_QUERY = """
query ($id: ID!) {
  order(id: $id) {
    refunds(first: 50) {
      id createdAt note
      totalRefundedSet { shopMoney { amount } }
      refundLineItems(first: 100) { nodes { quantity restockType location { id } lineItem { id } } }
      refundShippingLines(first: 10) {
        nodes { subtotalAmountSet { shopMoney { amount } } taxAmountSet { shopMoney { amount } } }
      }
      transactions(first: 20) { nodes { id kind status gateway processedAt amountSet { shopMoney { amount } } } }
    }
  }
}
"""

# Restock types meaning the item had shipped and comes back to a location.
RETURN_TYPES = {"RETURN", "LEGACY_RESTOCK"}


def _amount(money_set):
    return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0.0)


class ShopifyRefund(models.Model):
    _name = "shopify.refund"
    _description = "Shopify Refund"
    _order = "refund_date desc, id desc"
    _rec_name = "shopify_refund_id"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True, readonly=True)
    company_id = fields.Many2one(related="order_id.company_id")
    currency_id = fields.Many2one(related="order_id.currency_id")
    shopify_refund_id = fields.Char(string="Shopify Refund", required=True, readonly=True)
    refund_date = fields.Datetime(string="Date", readonly=True)
    amount = fields.Monetary(string="Refunded", readonly=True, help="Money Shopify paid back to the customer.")
    note = fields.Char(string="Shopify Note", readonly=True)
    credit_note_id = fields.Many2one("account.move", string="Credit Note", readonly=True, ondelete="set null")
    return_ids = fields.One2many("stock.picking", "shopify_refund_id", string="Returns", readonly=True)
    state = fields.Selection([("done", "Processed"), ("skipped", "Not Processed")], readonly=True, default="done")
    result = fields.Text(string="What Odoo Did", readonly=True)

    _refund_uniq = models.Constraint("unique(order_id, shopify_refund_id)", "This Shopify refund is already on the order.")


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shopify_refund_id = fields.Many2one("shopify.refund", string="Shopify Refund", readonly=True, copy=False,
                                        index="btree_not_null", ondelete="set null")


class AccountMove(models.Model):
    _inherit = "account.move"

    shopify_refund_id = fields.Char(string="Shopify Refund ID", readonly=True, copy=False, index="btree_not_null")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shopify_refund_ids = fields.One2many("shopify.refund", "order_id", string="Shopify Refunds", readonly=True)


class ShopifyStore(models.Model):
    _inherit = "shopify.store"

    process_refunds = fields.Boolean(
        string="Process Refunds", default=True,
        help="Follow cancellations and refunds made in Shopify: reduce or cancel deliveries, create credit "
             "notes and refund payments, and return receipts for restocked items.")
    refunds_from = fields.Datetime(
        string="Refunds From", default=fields.Datetime.now,
        help="Refunds made in Shopify before this time are listed on the order but not processed.")
    refund_product_id = fields.Many2one(
        "product.product", string="Refund Adjustment Product",
        help="Credit note line for the part of a refund that is not an item or shipping (goodwill, "
             "order discount, restocking fee). Empty: Shopify Refund Adjustment.")

    def write(self, vals):
        # Switching refunds back on never processes the refunds made while it was off.
        if vals.get("process_refunds") and "refunds_from" not in vals:
            self.filtered(lambda s: not s.process_refunds).refunds_from = fields.Datetime.now()
        return super().write(vals)

    # ------------------------------------------------------------------
    # Called by the order import for confirmed orders
    # ------------------------------------------------------------------
    def _shopify_follow_refunds(self, order, node, cancelled):
        """Process new refunds, then the cancellation, of a confirmed order."""
        if not self.process_refunds:
            if cancelled:
                self._shopify_add_issue(order, _(
                    "Cancelled in Shopify after the order was confirmed in Odoo. Cancel the delivery and order here."))
            return
        recorded = sum(order.shopify_refund_ids.mapped("amount"))
        if float_compare(_amount(node.get("totalRefundedSet")), recorded, precision_digits=2) > 0:
            for refund in self._shopify_fetch_refunds(order):
                self._shopify_process_refund_safely(order, refund)
        if cancelled:
            self._shopify_cancel_confirmed(order, node)

    def _shopify_fetch_refunds(self, order):
        try:
            data = self._call("import_orders", REFUNDS_QUERY, {"id": order.shopify_order_id}, order=order)
        except ShopifyError as error:
            raise UserError(_("Shopify refunds could not be read: %s", error)) from error
        return sorted((data.get("order") or {}).get("refunds") or [], key=lambda r: r.get("createdAt") or "")

    def _shopify_process_refund_safely(self, order, refund):
        if refund["id"] in order.shopify_refund_ids.mapped("shopify_refund_id"):
            return
        try:
            with self.env.cr.savepoint():
                self._shopify_process_refund(order, refund)
        except Exception as error:  # noqa: BLE001 - reported on the order; retried on the next import
            self._shopify_add_issue(order, _("A Shopify refund could not be processed: %s", error))

    def _shopify_process_refund(self, order, refund):
        Refund = self.env["shopify.refund"].sudo()
        values = {
            "order_id": order.id, "shopify_refund_id": refund["id"], "note": (refund.get("note") or "")[:250],
            "refund_date": _datetime(refund.get("createdAt")), "amount": _amount(refund.get("totalRefundedSet")),
        }
        if self.refunds_from and values["refund_date"] and values["refund_date"] < self.refunds_from:
            Refund.create({**values, "state": "skipped",
                           "result": _("Made in Shopify before refunds were followed; not processed.")})
            return
        record = Refund.create(values)
        done = []
        quantities = self._shopify_refund_quantities(order, refund)
        taken_off = self._shopify_take_off_order(order, quantities["unshipped"])
        if taken_off:
            done.append(_("Taken off the order before shipping: %s", taken_off))
        returns = self._shopify_create_returns(order, quantities["returned"], record)
        if returns:
            done.append(_("Return receipt %(returns)s for %(items)s, waiting for the warehouse.",
                          returns=", ".join(returns.mapped("name")), items=_describe(order, quantities["returned"])))
        if quantities["kept"]:
            done.append(_("Not coming back: %s", _describe(order, quantities["kept"])))
        credit_note = self._shopify_credit_note(order, refund, quantities["all"])
        if credit_note:
            record.credit_note_id = credit_note
            done.append(_("Credit note %(move)s for %(amount)s, refund paid in %(journal)s.",
                          move=credit_note.name, amount=f"{credit_note.amount_total:.2f}",
                          journal=self._shopify_payment_journal().name))
        elif values["amount"]:
            done.append(_("No posted invoice, so no credit note."))
        record.result = "\n".join(done) or _("Nothing to do in Odoo.")
        order.message_post(body=_("Refunded in Shopify (%(amount)s): %(result)s",
                                  amount=f"{values['amount']:.2f}", result=" ".join(done) or _("nothing to do.")))

    # ------------------------------------------------------------------
    # Quantities and stock
    # ------------------------------------------------------------------
    def _shopify_refund_quantities(self, order, refund):
        """Split each refunded item into not shipped yet, shipped and coming back, shipped and kept.
        Returns dicts sale line id -> quantity."""
        lines = {l.shopify_line_id: l for l in order.order_line if l.shopify_line_id}
        result = {key: defaultdict(float) for key in ("unshipped", "returned", "kept", "all")}
        for item in (refund.get("refundLineItems") or {}).get("nodes") or []:
            line = lines.get((item.get("lineItem") or {}).get("id"))
            qty = float(item.get("quantity") or 0)
            if not line or qty <= 0:
                continue
            result["all"][line.id] += qty
            unshipped = max(line.product_uom_qty - line.qty_delivered - result["unshipped"][line.id], 0.0)
            if item.get("restockType") == "CANCEL":
                result["unshipped"][line.id] += min(qty, unshipped)
                continue
            shipped = max(line.qty_delivered - result["returned"][line.id] - result["kept"][line.id], 0.0)
            from_shipped = min(qty, shipped)
            if from_shipped:
                result["returned" if item.get("restockType") in RETURN_TYPES else "kept"][line.id] += from_shipped
            if qty > from_shipped:
                result["unshipped"][line.id] += min(qty - from_shipped, unshipped)
        return {key: {k: v for k, v in values.items() if v > 0} for key, values in result.items()}

    def _shopify_take_off_order(self, order, quantities):
        """Lower the ordered quantity of items that will not ship; Odoo shrinks the open delivery."""
        if not quantities:
            return ""
        lines = self.env["sale.order.line"].browse(list(quantities))
        for line in lines:
            # Shrink the open delivery first: lowering the line alone makes Odoo plan a receipt instead.
            to_take = quantities[line.id]
            moves = line.move_ids.filtered(lambda m: m.state not in ("done", "cancel")
                                           and m.picking_code == "outgoing" and not m.origin_returned_move_id)
            for move in moves.sorted("id", reverse=True):
                take = min(to_take, move.product_uom._compute_quantity(move.product_uom_qty, line.product_uom_id))
                if take <= 0:
                    continue
                new_qty = move.product_uom_qty - line.product_uom_id._compute_quantity(take, move.product_uom)
                move._do_unreserve()
                if float_is_zero(new_qty, precision_rounding=move.product_uom.rounding):
                    move._action_cancel()
                else:
                    move.product_uom_qty = new_qty
                    move._action_assign()
                to_take -= take
            line.product_uom_qty = line.product_uom_qty - quantities[line.id]
        pickings = lines.move_ids.picking_id.filtered(lambda p: p.state != "cancel" and p.move_ids
                                                       and all(m.state == "cancel" for m in p.move_ids))
        pickings.action_cancel()
        return _describe(order, quantities)

    def _shopify_create_returns(self, order, quantities, record):
        """Return receipts from the deliveries that shipped the items, left Ready for the warehouse."""
        remaining = dict(quantities)
        returns = self.env["stock.picking"]
        deliveries = order.picking_ids.filtered(
            lambda p: p.state == "done" and p.picking_type_code == "outgoing" and not p.return_id)
        for delivery in deliveries.sorted("date_done", reverse=True):
            wanted = {}
            for move in delivery.move_ids.filtered(lambda m: m.state == "done" and m.sale_line_id.id in remaining):
                returned = sum(move.returned_move_ids.filtered(lambda m: m.state != "cancel").mapped("product_uom_qty"))
                qty = min(remaining[move.sale_line_id.id], move.quantity - returned)
                if qty > 0:
                    wanted[move.id] = qty
                    remaining[move.sale_line_id.id] -= qty
            if not wanted:
                continue
            wizard = self.env["stock.return.picking"].with_context(
                active_id=delivery.id, active_ids=delivery.ids, active_model="stock.picking").create({})
            for line in wizard.product_return_moves:
                line.write({"quantity": wanted.get(line.move_id.id, 0.0), "to_refund": True})
            new_picking = wizard._create_return()
            new_picking.write({"shopify_refund_id": record.id,
                               "origin": _("%s refund", order.shopify_order_name)})
            returns |= new_picking
            if not any(v > 0 for v in remaining.values()):
                break
        return returns

    # ------------------------------------------------------------------
    # Credit note and refund payment
    # ------------------------------------------------------------------
    def _shopify_credit_note(self, order, refund, quantities):
        transactions = [tx for tx in (refund.get("transactions") or {}).get("nodes") or []
                        if tx.get("kind") == "REFUND" and tx.get("status") == "SUCCESS" and _tx_amount(tx) > 0]
        money = sum(_tx_amount(tx) for tx in transactions)
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice" and m.state == "posted")[:1]
        currency = order.currency_id
        if float_is_zero(money, precision_rounding=currency.rounding) or not invoice:
            return False
        refund_date = min(_tx_date(tx) for tx in transactions)
        lines = []
        for line in self.env["sale.order.line"].browse(list(quantities)):
            lines.append(self._shopify_credit_line(line.product_id, quantities[line.id], line.price_unit,
                                                   line.tax_ids, name=line.name, discount=line.discount,
                                                   sale_line=line))
        shipping = sum(_amount(s.get("subtotalAmountSet")) + _amount(s.get("taxAmountSet"))
                       for s in (refund.get("refundShippingLines") or {}).get("nodes") or [])
        delivery_lines = order.order_line.filtered("is_delivery")
        if shipping > 0 and delivery_lines:
            charged = sum(delivery_lines.mapped("price_total"))
            line = delivery_lines[0]
            lines.append(self._shopify_credit_line(line.product_id, 1, min(shipping, charged), line.tax_ids,
                                                   name=line.name, sale_line=line))
        credit_note = self.env["account.move"].with_company(order.company_id).create({
            "move_type": "out_refund",
            "partner_id": invoice.partner_id.id,
            "journal_id": invoice.journal_id.id,
            "currency_id": currency.id,
            "invoice_date": refund_date,
            "invoice_origin": order.name,
            "ref": _("Shopify refund %s", order.shopify_order_name),
            "reversed_entry_id": invoice.id,
            "shopify_refund_id": refund["id"],
            "invoice_line_ids": [fields.Command.create(vals) for vals in lines],
        })
        difference = currency.round(money - credit_note.amount_total)
        if not currency.is_zero(difference):
            taxes = order.order_line.filtered(lambda l: not l.is_delivery and l.tax_ids)[:1].tax_ids
            product = self.refund_product_id or self.env.ref("js_shopify_connector.product_shopify_refund_adjustment")
            price = difference
            if taxes and not all(t.price_include for t in taxes):
                price = difference / (1 + sum(t.amount for t in taxes if t.amount_type == "percent") / 100)
            credit_note.invoice_line_ids = [fields.Command.create(self._shopify_credit_line(
                product, 1, price, taxes, name=_("Shopify refund adjustment")))]
        credit_note.action_post()
        self._shopify_register_refunds(order, credit_note, transactions)
        return credit_note

    @staticmethod
    def _shopify_credit_line(product, quantity, price, taxes, name, discount=0.0, sale_line=None):
        vals = {"product_id": product.id, "name": name, "quantity": quantity, "price_unit": price,
                "discount": discount, "tax_ids": [fields.Command.set(taxes.ids)]}
        if sale_line:
            vals["sale_line_ids"] = [fields.Command.set(sale_line.ids)]
        return vals

    def _shopify_register_refunds(self, order, credit_note, transactions):
        Payment = self.env["account.payment"].sudo()
        known = set(Payment.search([("shopify_transaction_id", "in", [tx["id"] for tx in transactions])])
                    .mapped("shopify_transaction_id"))
        journal = self._shopify_payment_journal()
        currency = order.currency_id
        for tx in sorted(transactions, key=lambda t: t.get("processedAt") or ""):
            if tx["id"] in known:
                continue
            amount = min(_tx_amount(tx), credit_note.amount_residual)
            if float_is_zero(amount, precision_rounding=currency.rounding):
                continue
            wizard = self.env["account.payment.register"].with_company(order.company_id).with_context(
                active_model="account.move", active_ids=credit_note.ids).create({
                    "journal_id": journal.id,
                    "amount": amount,
                    "payment_date": _tx_date(tx),
                    "communication": " ".join(filter(None, [order.shopify_order_name, "refund", tx.get("gateway")])),
                })
            wizard._create_payments().sudo().shopify_transaction_id = tx["id"]

    # ------------------------------------------------------------------
    # Cancellation of a confirmed order
    # ------------------------------------------------------------------
    def _shopify_cancel_confirmed(self, order, node):
        deliveries = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing" and not p.return_id)
        reason = (node.get("cancelReason") or "").lower()
        if not deliveries.filtered(lambda p: p.state == "done"):
            order.with_context(disable_cancel_warning=True)._action_cancel()
            order.message_post(body=_("Cancelled in Shopify%s; the order and its delivery are cancelled.",
                                      f" ({reason})" if reason else ""))
            return
        open_deliveries = deliveries.filtered(lambda p: p.state not in ("done", "cancel"))
        if open_deliveries:
            open_deliveries.action_cancel()
        self._shopify_add_issue(order, _(
            "Cancelled in Shopify after part of it was shipped. The deliveries not shipped yet are cancelled; "
            "check the shipped parcel and its refund."))


def _describe(order, quantities):
    lines = order.order_line.filtered(lambda l: l.id in quantities)
    return ", ".join(f"{quantities[l.id]:g} × {l.product_id.display_name}" for l in lines)


def _datetime(value):
    if not value:
        return False
    return fields.Datetime.to_datetime(value.replace("T", " ").replace("Z", "")[:19])
