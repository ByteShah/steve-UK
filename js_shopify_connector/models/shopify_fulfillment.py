"""Fulfilments and tracking back to Shopify (Task 3.4).

A validated outgoing delivery of a Shopify order becomes one Shopify fulfilment for the items and
quantities that left, with the tracking number. Sending happens in a cron that the validation
triggers straight away, so the warehouse is never blocked by Shopify; failures are retried.
"""
import logging
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

_logger = logging.getLogger(__name__)

FULFILLMENT_ORDERS_QUERY = """
query ($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 20) {
      nodes { id status assignedLocation { location { id } }
        lineItems(first: 100) { nodes { id remainingQuantity lineItem { id } } } }
    }
  }
}
"""

FO_NODE = "id status assignedLocation { location { id } } lineItems(first: 100) { nodes { id remainingQuantity lineItem { id } } }"

FULFILLMENT_ORDER_MOVE = """
mutation ($id: ID!, $newLocationId: ID!) {
  fulfillmentOrderMove(id: $id, newLocationId: $newLocationId) {
    movedFulfillmentOrder { %s }
    userErrors { field message }
  }
}
""" % FO_NODE

FULFILLMENT_CREATE = """
mutation ($fulfillment: FulfillmentInput!, $message: String) {
  fulfillmentCreate(fulfillment: $fulfillment, message: $message) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""

TRACKING_UPDATE = """
mutation ($fulfillmentId: ID!, $trackingInfoInput: FulfillmentTrackingInput!, $notifyCustomer: Boolean) {
  fulfillmentTrackingInfoUpdate(fulfillmentId: $fulfillmentId, trackingInfoInput: $trackingInfoInput,
                                notifyCustomer: $notifyCustomer) {
    fulfillment { id }
    userErrors { field message }
  }
}
"""

# Fulfilment orders that can still be fulfilled.
OPEN_FO = {"OPEN", "IN_PROGRESS"}


class ShopifySyncLog(models.Model):
    _inherit = "shopify.sync.log"

    operation = fields.Selection(selection_add=[("send_fulfillment", "Send Fulfilment")],
                                 ondelete={"send_fulfillment": "cascade"})


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    shopify_carrier_name = fields.Char(
        string="Shopify Carrier",
        help="Carrier name as Shopify knows it (e.g. Royal Mail, DPD UK, Evri, DHL Express), so Shopify "
             "builds the tracking link. Empty: the delivery method's name is sent.")


class ShopifyStore(models.Model):
    _inherit = "shopify.store"

    send_fulfillments = fields.Boolean(
        string="Send Fulfilments", default=True,
        help="When a delivery of a Shopify order is validated, mark the items fulfilled in Shopify "
             "with the tracking number.")
    notify_customer_on_shipment = fields.Boolean(
        string="Email Customer on Shipment", default=True,
        help="Shopify emails the customer its shipping confirmation with the tracking link.")

    @api.model
    def _cron_send_fulfillments(self):
        self._send_pending_fulfillments(commit=True)

    @api.model
    def _send_pending_fulfillments(self, commit=False):
        """Send deliveries waiting for Shopify, oldest first, and failed ones again."""
        pickings = self.env["stock.picking"].search(
            [("shopify_fulfillment_state", "in", ("to_send", "failed"))], order="date_done, id", limit=50)
        pickings._shopify_send_fulfillments(commit=commit)
        if len(pickings) == 50:
            self.env.ref("js_shopify_connector.ir_cron_shopify_send_fulfillments")._trigger()


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shopify_fulfillment_state = fields.Selection([
        ("to_send", "To Send"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("nothing", "Nothing to Send"),
    ], string="Shopify", readonly=True, copy=False, index="btree_not_null",
        help="Whether this delivery has reached Shopify as a fulfilment.")
    shopify_fulfillment_id = fields.Char(string="Shopify Fulfilment ID", readonly=True, copy=False)
    shopify_fulfillment_error = fields.Text(string="Shopify Message", readonly=True, copy=False)
    shopify_tracking_sent = fields.Char(string="Tracking Sent to Shopify", readonly=True, copy=False)

    def _shopify_store(self):
        order = self.sale_id
        return order.shopify_order_id and order.shopify_store_id

    def _shopify_is_fulfillable(self):
        store = self._shopify_store()
        return bool(store and store.active and store.send_fulfillments
                    and self.picking_type_code == "outgoing" and not self.return_id)

    def _action_done(self):
        res = super()._action_done()
        to_send = self.filtered(lambda p: p.state == "done" and p._shopify_is_fulfillable()
                                and not p.shopify_fulfillment_state)
        if to_send:
            to_send.shopify_fulfillment_state = "to_send"
            self._shopify_trigger_sending()
        return res

    def write(self, vals):
        if "carrier_tracking_ref" not in vals:
            return super().write(vals)
        before = {p.id: p.carrier_tracking_ref for p in self}
        res = super().write(vals)
        changed = self.filtered(lambda p: p.shopify_fulfillment_state == "sent" and p.shopify_fulfillment_id
                                and (p.carrier_tracking_ref or False) != (before[p.id] or False))
        if changed:
            changed.shopify_fulfillment_state = "to_send"
            self._shopify_trigger_sending()
        return res

    def _shopify_trigger_sending(self):
        cron = self.env.ref("js_shopify_connector.ir_cron_shopify_send_fulfillments", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def action_shopify_send_fulfillment(self):
        pickings = self.filtered(lambda p: p.shopify_fulfillment_state in ("to_send", "failed"))
        if not pickings:
            raise UserError(_("There is nothing to send to Shopify for this delivery."))
        pickings._shopify_send_fulfillments()
        failed = pickings.filtered(lambda p: p.shopify_fulfillment_state == "failed")
        if failed:
            message, kind = _("Shopify did not accept it: %s", failed[0].shopify_fulfillment_error), "danger"
        else:
            message, kind = _("Sent to Shopify."), "success"
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": kind, "message": message, "sticky": kind == "danger",
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _shopify_send_fulfillments(self, commit=False):
        for picking in self.sudo():
            # One sender per delivery: a second one (cron and button together) would fulfil twice.
            self.env.cr.execute("SELECT id FROM stock_picking WHERE id = %s FOR UPDATE SKIP LOCKED", [picking.id])
            if not self.env.cr.fetchone():
                continue
            picking.invalidate_recordset(["shopify_fulfillment_state"])
            if picking.shopify_fulfillment_state not in ("to_send", "failed"):
                continue
            try:
                with self.env.cr.savepoint():
                    if picking.shopify_fulfillment_id:
                        picking._shopify_update_tracking()
                    else:
                        picking._shopify_create_fulfillment()
            except Exception as error:  # noqa: BLE001 - shown on the delivery, retried by the cron
                _logger.info("Shopify fulfilment of %s failed: %s", picking.name, error)
                # The savepoint rolled back the call's own log entry: keep the failure visible.
                self.env["shopify.sync.log"]._log(
                    picking._shopify_store(), "send_fulfillment", "error",
                    _("%(delivery)s: %(error)s", delivery=picking.name, error=error),
                    response=getattr(error, "response", None), order=picking.sale_id)
                picking.write({"shopify_fulfillment_state": "failed",
                               "shopify_fulfillment_error": picking._shopify_explain(error)})
            if commit:
                self.env.cr.commit()

    def _shopify_quantities(self):
        """Quantity that left, per Shopify line item, in the order line's unit."""
        quantities = defaultdict(float)
        for move in self.move_ids.filtered(lambda m: m.state == "done" and m.sale_line_id.shopify_line_id):
            line = move.sale_line_id
            quantities[line.shopify_line_id] += move.product_uom._compute_quantity(
                move.quantity, line.product_uom_id)
        return {key: int(float_round(qty, precision_digits=0)) for key, qty in quantities.items()
                if float_round(qty, precision_digits=0) > 0}

    def _shopify_tracking_info(self):
        if not self.carrier_tracking_ref:
            return None
        info = {"number": self.carrier_tracking_ref}
        carrier = self.carrier_id
        if carrier:
            info["company"] = carrier.shopify_carrier_name or carrier.name
        url = self.carrier_tracking_url
        if url and url.startswith("http"):
            info["url"] = url
        return info

    def _shopify_create_fulfillment(self):
        store, order = self._shopify_store(), self.sale_id
        wanted = self._shopify_quantities()
        if not wanted:
            return self._shopify_nothing(_("No Shopify items on this delivery."))
        data = store._call("send_fulfillment", FULFILLMENT_ORDERS_QUERY, {"id": order.shopify_order_id},
                           order=order)
        fulfillment_orders = ((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes") or []
        needed = [fo for fo in fulfillment_orders
                  if any(li["lineItem"]["id"] in wanted and li["remainingQuantity"] > 0
                         for li in fo["lineItems"]["nodes"])]
        on_hold = [fo for fo in needed if fo["status"] == "ON_HOLD"]
        if on_hold:
            raise UserError(_("The order is on hold in Shopify. Release the hold there, then click Send to Shopify."))
        waiting = [fo for fo in needed if fo["status"] in ("SCHEDULED", "INCOMPLETE")]
        if waiting:
            raise UserError(_("Shopify cannot fulfil the order yet (status %s). Check it in Shopify, "
                              "then click Send to Shopify.", waiting[0]["status"].lower()))
        needed = [fo for fo in needed if fo["status"] in OPEN_FO]
        if not needed:
            return self._shopify_nothing(_("Already fulfilled in Shopify; nothing was sent."))

        location = self.picking_type_id.warehouse_id.shopify_location_id
        if location and location.store_id == store:
            needed = [self._shopify_move_fulfillment_order(store, order, fo, location) for fo in needed]

        remaining = dict(wanted)
        by_fulfillment_order = []
        for fo in needed:
            items = []
            for li in fo["lineItems"]["nodes"]:
                qty = min(remaining.get(li["lineItem"]["id"], 0), li["remainingQuantity"])
                if qty > 0:
                    items.append({"id": li["id"], "quantity": qty})
                    remaining[li["lineItem"]["id"]] -= qty
            if items:
                by_fulfillment_order.append({"fulfillmentOrderId": fo["id"], "fulfillmentOrderLineItems": items})
        if not by_fulfillment_order:
            return self._shopify_nothing(_("Already fulfilled in Shopify; nothing was sent."))

        fulfillment = {"lineItemsByFulfillmentOrder": by_fulfillment_order,
                       "notifyCustomer": store.notify_customer_on_shipment}
        tracking = self._shopify_tracking_info()
        if tracking:
            fulfillment["trackingInfo"] = tracking
        result = store._call("send_fulfillment", FULFILLMENT_CREATE,
                             {"fulfillment": fulfillment, "message": self.name},
                             mutation="fulfillmentCreate", order=order)
        self.write({"shopify_fulfillment_state": "sent",
                    "shopify_fulfillment_id": (result.get("fulfillment") or {}).get("id"),
                    "shopify_fulfillment_error": False,
                    "shopify_tracking_sent": self.carrier_tracking_ref or False})
        sent = {key: qty - remaining.get(key, 0) for key, qty in wanted.items()}
        lines = order.order_line.filtered(lambda l: sent.get(l.shopify_line_id))
        summary = ", ".join(f"{sent[l.shopify_line_id]} × {l.product_id.display_name}" for l in lines)
        if tracking:
            summary += " · " + " ".join(filter(None, [tracking.get("company"), tracking["number"]]))
        message = _("Fulfilled in Shopify: %s", summary)
        left = {key: qty for key, qty in remaining.items() if qty > 0}
        if left:
            message += " " + _("(%s item(s) were already fulfilled in Shopify and were not sent again.)",
                               sum(left.values()))
        self.message_post(body=message)
        order.message_post(body=_("%(delivery)s: %(message)s", delivery=self.name, message=message))

    def _shopify_move_fulfillment_order(self, store, order, fulfillment_order, location):
        assigned = ((fulfillment_order.get("assignedLocation") or {}).get("location") or {}).get("id")
        if assigned == location.shopify_id:
            return fulfillment_order
        result = store._call("send_fulfillment", FULFILLMENT_ORDER_MOVE,
                             {"id": fulfillment_order["id"], "newLocationId": location.shopify_id},
                             mutation="fulfillmentOrderMove", order=order)
        return result.get("movedFulfillmentOrder") or fulfillment_order

    def _shopify_update_tracking(self):
        store, order = self._shopify_store(), self.sale_id
        tracking = self._shopify_tracking_info()
        if (self.carrier_tracking_ref or False) == (self.shopify_tracking_sent or False) or not tracking:
            self.write({"shopify_fulfillment_state": "sent", "shopify_fulfillment_error": False})
            return
        store._call("send_fulfillment", TRACKING_UPDATE,
                    {"fulfillmentId": self.shopify_fulfillment_id, "trackingInfoInput": tracking,
                     "notifyCustomer": store.notify_customer_on_shipment},
                    mutation="fulfillmentTrackingInfoUpdate", order=order)
        self.write({"shopify_fulfillment_state": "sent", "shopify_fulfillment_error": False,
                    "shopify_tracking_sent": self.carrier_tracking_ref})
        message = _("Tracking updated in Shopify: %s",
                    " ".join(filter(None, [tracking.get("company"), tracking["number"]])))
        self.message_post(body=message)
        order.message_post(body=_("%(delivery)s: %(message)s", delivery=self.name, message=message))

    def _shopify_nothing(self, reason):
        self.write({"shopify_fulfillment_state": "nothing", "shopify_fulfillment_error": reason})
        self.message_post(body=reason)

    def _shopify_explain(self, error):
        text = str(error)
        if "access denied" in text.lower() and "fulfillment" in text.lower():
            return self.env._(
                "The Shopify app has no fulfilment permission (read/write_merchant_managed_fulfillment_orders). "
                "Grant it, then this delivery is sent automatically. Shopify said: %s", text)
        return text
