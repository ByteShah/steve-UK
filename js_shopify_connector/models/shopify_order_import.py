"""Import Shopify orders as sales orders of the store's company (Task 3.1)."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

from ..services.shopify_client import ShopifyError

_logger = logging.getLogger(__name__)

ADDRESS = "name firstName lastName company address1 address2 city zip province provinceCode countryCodeV2 phone"

ORDER_FIELDS = """
  id name createdAt updatedAt cancelledAt test
  displayFinancialStatus displayFulfillmentStatus
  currencyCode taxesIncluded note email phone
  customer { id email firstName lastName phone }
  shippingAddress { %s }
  billingAddress { %s }
  totalPriceSet { shopMoney { amount currencyCode } }
  lineItems(first: 100) {
    nodes { id sku name quantity variant { id }
      originalUnitPriceSet { shopMoney { amount } }
      discountAllocations { allocatedAmountSet { shopMoney { amount } } } }
    pageInfo { hasNextPage }
  }
  transactions(first: 50) { id kind status gateway processedAt amountSet { shopMoney { amount } } }
  shippingLines(first: 10) {
    nodes { title code originalPriceSet { shopMoney { amount } }
      discountAllocations { allocatedAmountSet { shopMoney { amount } } } }
  }
""" % (ADDRESS, ADDRESS)

# 25 orders of up to 100 items cost about 650 of the 1000 points a single query may use.
ORDERS_QUERY = """
query ($after: String, $query: String) {
  orders(first: 25, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
""" % ORDER_FIELDS

ORDER_QUERY = """
query ($id: ID!) { order(id: $id) { %s } }
""" % ORDER_FIELDS

# Shopify payment status -> confirm the sales order automatically.
CONFIRM_STATUSES = {"paid", "authorized", "partially_paid", "partially_refunded"}
# The order will not go ahead.
CANCEL_STATUSES = {"voided", "expired"}
OVERLAP = timedelta(minutes=1)


def _money(money_set):
    return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0.0)


def _allocated(node):
    return sum(_money(a.get("allocatedAmountSet")) for a in node.get("discountAllocations") or [])


def _shopify_datetime(value):
    """'2026-11-03T08:15:00Z' -> naive UTC datetime, as Odoo stores it."""
    if not value:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


class ShopifyStore(models.Model):
    _inherit = "shopify.store"

    import_orders = fields.Boolean(
        string="Import Orders", help="Import Shopify orders as sales orders every 5 minutes.")
    orders_import_from = fields.Datetime(
        string="Import Orders From", help="Orders placed or changed before this time are not imported.")
    orders_synced_until = fields.Datetime(
        string="Orders Imported Until", readonly=True, copy=False,
        help="Orders changed in Shopify up to this time have been imported.")
    import_test_orders = fields.Boolean(
        string="Import Test Orders", help="Also import orders paid with Shopify's test gateway. Use on a dev store only.")
    auto_confirm_orders = fields.Boolean(
        string="Confirm Paid Orders", default=True,
        help="Confirm paid and authorised orders automatically, so the delivery is created.")
    order_team_id = fields.Many2one(
        "crm.team", string="Sales Team", default=lambda self: self.env.ref(
            "js_shopify_connector.crm_team_shopify", raise_if_not_found=False))
    order_pricelist_id = fields.Many2one(
        "product.pricelist", string="Price List", check_company=True,
        help="Price list set on imported orders, e.g. GBP Retail. Prices are those charged by Shopify.")
    order_warehouse_id = fields.Many2one(
        "stock.warehouse", string="Ship From", check_company=True,
        help="Warehouse that delivers Shopify orders. Empty: the warehouse linked to the Shopify location.")
    order_shipping_product_id = fields.Many2one(
        "product.product", string="Shipping Product", default=lambda self: self.env.ref(
            "js_shopify_connector.product_shopify_shipping", raise_if_not_found=False))
    order_discount_product_id = fields.Many2one(
        "product.product", string="Discount Product", default=lambda self: self.env.ref(
            "js_shopify_connector.product_shopify_discount", raise_if_not_found=False))
    order_responsible_id = fields.Many2one(
        "res.users", string="Notify", default=lambda self: self.env.user,
        help="Gets an activity when a Shopify order needs attention.")
    order_count = fields.Integer(compute="_compute_order_count")

    def _compute_order_count(self):
        counts = dict(self.env["sale.order"]._read_group(
            [("shopify_store_id", "in", self.ids)], ["shopify_store_id"], ["__count"]))
        for store in self:
            store.order_count = counts.get(store, 0)

    def write(self, vals):
        res = super().write(vals)
        if vals.get("import_orders"):
            self.filtered(lambda s: not s.orders_import_from).orders_import_from = fields.Datetime.now()
        return res

    # ------------------------------------------------------------------
    # Buttons and job
    # ------------------------------------------------------------------
    def action_import_orders_now(self):
        self.ensure_one()
        if not self.import_orders:
            return self._notify(_("Turn on Import Orders on the Orders tab first."), "warning")
        result = self._import_orders()
        if result is False:
            return self._notify(_("Shopify could not be read. See the sync log."), "danger")
        created, updated, attention = result
        message = _("%(created)s new and %(updated)s updated Shopify order(s).", created=created, updated=updated)
        if attention:
            message += " " + _("%s need attention.", attention)
        return self._notify(message, "warning" if attention else "success")

    def action_view_orders(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Shopify Orders"),
            "res_model": "sale.order",
            "view_mode": "list,form",
            "domain": [("shopify_store_id", "=", self.id)],
            "context": {"create": False},
        }

    @api.model
    def _cron_import_orders(self):
        store = self._get_active_store()
        if store and store.import_orders:
            store._import_orders(commit=True)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def _import_orders(self, commit=False):
        """Import orders created or changed since the last run, oldest change first.
        Returns (created, updated, needing attention), or False when Shopify could not be read."""
        self.ensure_one()
        since = self.orders_synced_until or self.orders_import_from or fields.Datetime.now()
        query = "updated_at:>='%s'" % (since - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
        created = updated = attention = 0
        after = None
        while True:
            try:
                page = self._call("import_orders", ORDERS_QUERY, {"after": after, "query": query})["orders"]
            except ShopifyError:
                # Logged by _call; the next run starts again from the last order processed.
                return False
            for node in page["nodes"]:
                order, is_new = self._shopify_process_order_safely(node)
                if order:
                    created += is_new
                    updated += not is_new
                    attention += bool(order.shopify_import_issue)
                changed = _shopify_datetime(node.get("updatedAt"))
                if changed and (not self.orders_synced_until or changed > self.orders_synced_until):
                    self.orders_synced_until = changed
                if commit:
                    self.env["ir.cron"]._commit_progress(1)
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        if not self.orders_synced_until:
            self.orders_synced_until = since
        return created, updated, attention

    def _shopify_import_order_by_id(self, shopify_order_id):
        """Fetch one order and rebuild its quotation. Returns the sales order."""
        self.ensure_one()
        node = self._shopify_fetch_order(shopify_order_id)
        if not node:
            raise UserError(_("Shopify no longer has this order."))
        return self._shopify_process_order(node, rebuild=True)[0]

    def _shopify_fetch_order(self, shopify_order_id):
        try:
            return self._call("import_orders", ORDER_QUERY, {"id": shopify_order_id})["order"]
        except ShopifyError as error:
            raise UserError(_("Shopify could not be read: %s", error)) from error

    def _shopify_process_order_safely(self, node):
        try:
            with self.env.cr.savepoint():
                return self._shopify_process_order(node)
        except Exception as error:  # noqa: BLE001 - one broken order must not stop the others
            _logger.exception("Import of Shopify order %s failed", node.get("name"))
            self.env["shopify.sync.log"]._log(
                self, "import_orders", "error", _("Order %(name)s could not be imported: %(error)s",
                                                  name=node.get("name"), error=error), request=node)
            return False, False

    def _shopify_process_order(self, node, rebuild=False):
        """Create or update the sales order for one Shopify order. Returns (order, created)."""
        self.ensure_one()
        SaleOrder = self.env["sale.order"].with_company(self.company_id).with_context(
            mail_create_nosubscribe=True)
        order = SaleOrder.search([("shopify_store_id", "=", self.id), ("shopify_order_id", "=", node["id"])], limit=1)
        status = (node.get("displayFinancialStatus") or "").lower() or False
        cancelled = bool(node.get("cancelledAt")) or status in CANCEL_STATUSES

        if not order:
            if node.get("test") and not self.import_test_orders:
                return False, False
            if cancelled:
                return False, False
            order = SaleOrder.create(self._shopify_order_values(node))
            self._shopify_build_lines(order, node)
            self._shopify_after_import(order, node, status)
            self.env["shopify.sync.log"]._log(self, "import_orders", "success", _(
                "Imported %(shopify)s as %(order)s.", shopify=node["name"], order=order.name), order=order)
            return order, True

        order.write(self._shopify_status_values(node))
        if order.state in ("draft", "sent"):
            if cancelled:
                order._action_cancel()
                order.message_post(body=_("Cancelled in Shopify."))
            else:
                if rebuild:
                    order.order_line.unlink()
                    order.carrier_id = False
                    order.partner_invoice_id = self._shopify_order_address(
                        order.partner_id, self._shopify_address_values(node.get("billingAddress")), "invoice")
                    self._shopify_build_lines(order, node)
                self._shopify_sync_delivery_address(order, node)
                self._shopify_after_import(order, node, status)
        elif order.state == "sale" and cancelled:
            # Cancelling a confirmed order (stock, invoice, delivery) is Task 3.5: tell a person.
            self._shopify_add_issue(order, _(
                "Cancelled in Shopify after the order was confirmed in Odoo. Cancel the delivery and order here."))
        elif order.state == "sale":
            self._shopify_sync_delivery_address(order, node)
            self._shopify_invoice_and_pay(order, node)
        return order, False

    def _shopify_status_values(self, node):
        status = (node.get("displayFinancialStatus") or "").lower()
        fulfillment = (node.get("displayFulfillmentStatus") or "").lower()
        return {
            "shopify_financial_status": status if status in dict(self.env["sale.order"]._fields[
                "shopify_financial_status"].selection) else False,
            "shopify_fulfillment_status": fulfillment if fulfillment in dict(self.env["sale.order"]._fields[
                "shopify_fulfillment_status"].selection) else False,
            "shopify_total": _money(node.get("totalPriceSet")),
            "shopify_updated_at": _shopify_datetime(node.get("updatedAt")),
        }

    def _shopify_order_values(self, node):
        partner = self._shopify_find_partner(node)
        warehouse = self.order_warehouse_id or self._get_stock_warehouses()[:1]
        vals = {
            "company_id": self.company_id.id,
            **self._shopify_address_order_values(partner, node),
            "date_order": _shopify_datetime(node.get("createdAt")) or fields.Datetime.now(),
            "client_order_ref": node["name"],
            "origin": _("Shopify %s", node["name"]),
            "user_id": False,
            "team_id": (self.order_team_id or self.env.ref(
                "js_shopify_connector.crm_team_shopify", raise_if_not_found=False)).id,
            "shopify_store_id": self.id,
            "shopify_order_id": node["id"],
            "shopify_order_name": node["name"],
            **self._shopify_status_values(node),
        }
        if warehouse:
            vals["warehouse_id"] = warehouse.id
        if self.order_pricelist_id:
            vals["pricelist_id"] = self.order_pricelist_id.id
        return vals

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------
    def _shopify_find_variant(self, item):
        Variant = self.env["product.product"].with_context(active_test=False)
        variant = Variant.browse()
        if (item.get("variant") or {}).get("id"):
            variant = Variant.search([("shopify_variant_id", "=", item["variant"]["id"])], limit=1)
        if not variant and item.get("sku"):
            variant = Variant.search([("default_code", "=", item["sku"])], limit=1)
        return variant

    def _shopify_line_taxes(self, product, order):
        taxes = product.taxes_id._filter_taxes_by_company(order.company_id)
        return taxes or order.company_id.account_sale_tax_id

    def _shopify_build_lines(self, order, node):
        """Product lines at the prices charged, one discount line per tax, shipping lines.
        Problems are stored on the order as `shopify_import_issue`."""
        Line = self.env["sale.order.line"].with_company(order.company_id)
        currency = order.currency_id
        issues, discounted = [], []
        for item in node["lineItems"]["nodes"]:
            quantity = item.get("quantity") or 0
            price = _money(item.get("originalUnitPriceSet"))
            variant = self._shopify_find_variant(item)
            if not variant:
                issues.append(_("Not found in Odoo: %(name)s (SKU %(sku)s).",
                                name=item.get("name"), sku=item.get("sku") or "-"))
                Line.create({"order_id": order.id, "display_type": "line_note",
                             "name": _("Shopify item not found in Odoo: %(name)s, SKU %(sku)s, %(qty)s × %(price)s",
                                       name=item.get("name"), sku=item.get("sku") or "-", qty=quantity,
                                       price=price)})
                continue
            line = Line.create({"order_id": order.id, "product_id": variant.id, "product_uom_qty": quantity,
                                "price_unit": price, "shopify_line_id": item["id"]})
            discount = _allocated(item)
            if not float_is_zero(discount, precision_rounding=currency.rounding):
                discounted.append((line, discount))
        if node["lineItems"]["pageInfo"].get("hasNextPage"):
            issues.append(_("The order has more than 100 items; only the first 100 were imported."))

        # Shopify spreads line and order discount codes over the lines (VAT included). One line per
        # tax keeps the total exact and the VAT right on mixed baskets.
        by_taxes = defaultdict(float)
        for line, discount in discounted:
            by_taxes[line.tax_ids] += discount
        discount_product = self.order_discount_product_id or self.env.ref(
            "js_shopify_connector.product_shopify_discount", raise_if_not_found=False)
        for taxes, amount in by_taxes.items():
            if not discount_product:
                issues.append(_("Set a Discount Product on the Shopify store."))
                break
            Line.create({"order_id": order.id, "product_id": discount_product.id, "product_uom_qty": 1,
                         "price_unit": -amount, "tax_ids": [(6, 0, taxes.ids)],
                         "name": _("Shopify discount") + (f" ({', '.join(taxes.mapped('name'))})" if len(by_taxes) > 1 else "")})

        for shipping in node.get("shippingLines", {}).get("nodes") or []:
            shipping_product = self._shopify_shipping_product(shipping.get("title"), order)
            if not shipping_product:
                issues.append(_("Set a Shipping Product on the Shopify store."))
                break
            Line.create({"order_id": order.id, "product_id": shipping_product.id, "product_uom_qty": 1,
                         "price_unit": _money(shipping.get("originalPriceSet")) - _allocated(shipping),
                         "tax_ids": [(6, 0, self._shopify_line_taxes(shipping_product, order).ids)],
                         "name": shipping.get("title") or shipping_product.name, "is_delivery": True})

        if node.get("currencyCode") and node["currencyCode"] != currency.name:
            issues.append(_("The order is in %(shopify)s but the price list is in %(odoo)s.",
                            shopify=node["currencyCode"], odoo=currency.name))
        total = _money(node.get("totalPriceSet"))
        if float_compare(order.amount_total, total, precision_rounding=currency.rounding) and not issues:
            issues.append(_("The Odoo total %(odoo)s differs from the Shopify total %(shopify)s. "
                            "Check the taxes of the products.",
                            odoo=f"{order.amount_total:.2f}", shopify=f"{total:.2f}"))
        order.shopify_import_issue = "\n".join(issues) or False
        if node.get("note"):
            order.message_post(body=_("Customer note from Shopify: %s", node["note"]))

    def _shopify_after_import(self, order, node, status):
        """Confirm, or tell a person why not."""
        if order.shopify_import_issue:
            self._shopify_flag(order, order.shopify_import_issue)
            return
        if not (self.auto_confirm_orders and status in CONFIRM_STATUSES):
            return  # e.g. payment pending: stays a quotation until Shopify reports it paid
        try:
            with self.env.cr.savepoint():
                order.with_company(order.company_id).action_confirm()
        except UserError as error:
            order.shopify_import_issue = _("Could not be confirmed: %s", error)
            self._shopify_flag(order, order.shopify_import_issue)
            return
        self._shopify_invoice_and_pay(order, node)

    def _shopify_add_issue(self, order, issue):
        """Add a problem found after the order was imported, once, and tell a person."""
        issues = (order.shopify_import_issue or "").splitlines()
        if issue not in issues:
            order.shopify_import_issue = "\n".join(issues + [issue])
            self._shopify_flag(order, issue)

    def _shopify_flag(self, order, issue):
        user = self.order_responsible_id or self.env.ref("base.user_admin", raise_if_not_found=False)
        summary = _("Shopify order needs attention")
        if user and not order.activity_ids.filtered(lambda a: a.summary == summary):
            order.activity_schedule("mail.mail_activity_data_todo", summary=summary, note=issue, user_id=user.id)
