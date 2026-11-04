from datetime import datetime

from odoo.tests import tagged

from .test_shopify_connection import FakeResponse, gql
from .test_shopify_publish import ShopifyPublishCase

NATURAL_GID = "gid://shopify/ProductVariant/1"
BLACK_GID = "gid://shopify/ProductVariant/2"


@tagged("post_install", "-at_install")
class TestShopifyOrders(ShopifyPublishCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        # UK prices include VAT, as on Shopify.
        cls.vat_incl = env["account.tax"].create({
            "name": "VAT 20% incl. (test)", "amount": 20, "amount_type": "percent", "type_tax_use": "sale",
            "price_include_override": "tax_included"})
        cls.template.taxes_id = cls.vat_incl
        cls.natural.shopify_variant_id = NATURAL_GID
        cls.black = cls.template.product_variant_ids - cls.natural
        cls.black.shopify_variant_id = BLACK_GID
        cls.store.write({"import_orders": True, "orders_import_from": datetime(2026, 11, 1),
                         "order_warehouse_id": cls.warehouse.id})
        cls.store.order_shipping_product_id.taxes_id = cls.vat_incl
        cls.currency = env.company.currency_id.name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def order_node(self, number=1001, status="PAID", items=None, shipping=4.95, total=None, **extra):
        """A Shopify order as the orders query returns it.
        items: (variant gid or None, sku, quantity, unit price, discount)."""
        items = items if items is not None else [(NATURAL_GID, "SN-CDB-NAT", 1, 195.0, 0.0)]
        nodes = []
        for n, (variant, sku, qty, price, discount) in enumerate(items, start=1):
            nodes.append({
                "id": f"gid://shopify/LineItem/{number}{n}", "sku": sku, "name": f"Test Cane Bed - {sku}",
                "quantity": qty, "variant": {"id": variant} if variant else None,
                "originalUnitPriceSet": {"shopMoney": {"amount": f"{price:.2f}"}},
                "discountAllocations": [{"allocatedAmountSet": {"shopMoney": {"amount": f"{discount:.2f}"}}}]
                if discount else [],
            })
        if total is None:
            total = sum(qty * price - discount for _v, _s, qty, price, discount in items) + (shipping or 0)
        node = {
            "id": f"gid://shopify/Order/{number}", "name": f"#{number}",
            "createdAt": "2026-11-03T09:00:00Z", "updatedAt": "2026-11-03T09:05:00Z",
            "cancelledAt": None, "test": False,
            "displayFinancialStatus": status, "displayFulfillmentStatus": "UNFULFILLED",
            "currencyCode": self.currency, "taxesIncluded": True, "note": None,
            "email": "jane@example.com", "phone": None,
            "customer": {"id": "gid://shopify/Customer/7", "email": "jane@example.com",
                         "firstName": "Jane", "lastName": "Doe", "phone": "+447700900123"},
            "shippingAddress": {"firstName": "Jane", "lastName": "Doe", "company": None,
                                "address1": "10 Downing Street", "address2": None, "city": "London",
                                "zip": "SW1A 2AA", "provinceCode": None, "countryCodeV2": "GB", "phone": None},
            "billingAddress": None,
            "totalPriceSet": {"shopMoney": {"amount": f"{total:.2f}", "currencyCode": self.currency}},
            "lineItems": {"nodes": nodes, "pageInfo": {"hasNextPage": False}},
            "shippingLines": {"nodes": [{
                "title": "Standard", "code": "standard",
                "originalPriceSet": {"shopMoney": {"amount": f"{shipping:.2f}"}}, "discountAllocations": []}]
                if shipping is not None else []},
        }
        node.update(extra)
        return node

    @staticmethod
    def page(*nodes):
        return gql({"orders": {"nodes": list(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}}})

    def run_import(self, *nodes):
        return self.run_shopify(self.store._import_orders, self.page(*nodes))

    def find(self, number=1001):
        return self.env["sale.order"].search([("shopify_order_id", "=", f"gid://shopify/Order/{number}")])

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_paid_order_confirmed(self):
        result, calls = self.run_import(self.order_node())
        self.assertEqual(result, (1, 0, 0))
        order = self.find()
        self.assertEqual(order.state, "sale", "A paid order is confirmed.")
        self.assertFalse(order.shopify_import_issue)
        self.assertEqual((order.client_order_ref, order.shopify_order_name), ("#1001", "#1001"))
        self.assertEqual(order.company_id, self.store.company_id)
        self.assertEqual(order.warehouse_id, self.warehouse)
        self.assertEqual(order.team_id, self.env.ref("js_shopify_connector.crm_team_shopify"))
        self.assertEqual(order.shopify_financial_status, "paid")
        self.assertEqual(order.date_order, datetime(2026, 11, 3, 9, 0))

        product_line = order.order_line.filtered(lambda l: l.product_id == self.natural)
        self.assertEqual((product_line.product_uom_qty, product_line.price_unit), (1, 195.0))
        self.assertEqual(product_line.tax_ids, self.vat_incl, "The product's tax for the store company.")
        shipping = order.order_line.filtered("is_delivery")
        self.assertEqual((shipping.name, shipping.price_unit), ("Standard", 4.95))
        self.assertAlmostEqual(order.amount_total, 199.95)
        self.assertTrue(order.picking_ids, "Confirming creates the delivery.")

        customer = order.partner_id
        self.assertEqual((customer.name, customer.email, customer.shopify_customer_id),
                         ("Jane Doe", "jane@example.com", "gid://shopify/Customer/7"))
        self.assertEqual((customer.city, customer.zip, customer.country_id.code), ("London", "SW1A 2AA", "GB"))
        self.assertEqual(self.store.orders_synced_until, datetime(2026, 11, 3, 9, 5))
        query = self.body(calls[0])["variables"]["query"]
        self.assertEqual(query, "updated_at:>='2026-10-31T23:59:00Z'",
                         "First run starts at Import Orders From, with a one-minute overlap.")

    def test_rerun_never_duplicates(self):
        self.run_import(self.order_node())
        result, calls = self.run_import(self.order_node(displayFulfillmentStatus="FULFILLED"))
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(self.find()), 1)
        self.assertEqual(self.find().shopify_fulfillment_status, "fulfilled")
        self.assertIn("2026-11-03T09:04:00Z", self.body(calls[0])["variables"]["query"],
                      "The next run starts at the last change seen, minus the overlap.")

    def test_pending_order_waits_then_confirms(self):
        node = self.order_node(status="PENDING", items=[(NATURAL_GID, "SN-CDB-NAT", 2, 195.0, 0.0)])
        self.run_import(node)
        order = self.find()
        self.assertEqual(order.state, "draft", "Waiting for payment: stays a quotation.")
        quantities = self.template._shopify_location_quantities(self.store, self.natural)
        self.assertEqual(quantities[self.natural.id]["gid://shopify/Location/1"], 22,
                         "24 in stock, 2 held for the pending Shopify order.")

        self.run_import(dict(node, displayFinancialStatus="PAID", updatedAt="2026-11-03T10:00:00Z"))
        self.assertEqual(order.state, "sale", "Confirmed once Shopify reports it paid.")
        quantities = self.template._shopify_location_quantities(self.store, self.natural)
        self.assertEqual(quantities[self.natural.id]["gid://shopify/Location/1"], 22,
                         "Now reserved by the delivery instead: still 22.")

    def test_pending_then_voided_is_cancelled(self):
        node = self.order_node(status="PENDING")
        self.run_import(node)
        self.run_import(dict(node, displayFinancialStatus="VOIDED", updatedAt="2026-11-03T10:00:00Z"))
        self.assertEqual(self.find().state, "cancel")

    def test_cancelled_and_test_orders_are_skipped(self):
        cancelled = self.order_node(1002, cancelledAt="2026-11-03T09:01:00Z")
        test_order = self.order_node(1003, test=True)
        result, _calls = self.run_import(cancelled, test_order)
        self.assertEqual(result, (0, 0, 0))
        self.assertFalse(self.find(1002) or self.find(1003))
        self.store.import_test_orders = True
        self.run_import(test_order)
        self.assertTrue(self.find(1003), "Test orders come in when Import Test Orders is on.")

    def test_unknown_sku_needs_attention(self):
        node = self.order_node(items=[(None, "NOT-IN-ODOO", 1, 50.0, 0.0)])
        result, _calls = self.run_import(node)
        self.assertEqual(result, (1, 0, 1))
        order = self.find()
        self.assertEqual(order.state, "draft", "Never confirmed with a missing product.")
        self.assertIn("NOT-IN-ODOO", order.shopify_import_issue)
        self.assertTrue(order.order_line.filtered(lambda l: l.display_type == "line_note"))
        self.assertTrue(order.activity_ids, "The Shopify manager gets an activity.")

        # Fix the product in Odoo, then re-import the quotation from Shopify.
        self.black.default_code = "NOT-IN-ODOO"
        self.run_shopify(order.action_shopify_reimport, gql({"order": node}))
        self.assertFalse(order.shopify_import_issue)
        self.assertIn(self.black, order.order_line.product_id)
        self.assertFalse(order.order_line.filtered(lambda l: l.display_type == "line_note"))
        self.assertEqual(order.state, "sale", "Everything matches now, and it was paid: confirmed.")

    def test_total_difference_needs_attention(self):
        self.run_import(self.order_node(total=180.0))
        order = self.find()
        self.assertEqual(order.state, "draft")
        self.assertIn("differs from the Shopify total 180.00", order.shopify_import_issue)

    def test_discounts_become_one_line_per_tax(self):
        node = self.order_node(items=[(NATURAL_GID, "SN-CDB-NAT", 2, 195.0, 39.0)])
        self.run_import(node)
        order = self.find()
        discount = order.order_line.filtered(
            lambda l: l.product_id == self.store.order_discount_product_id)
        self.assertEqual((discount.price_unit, discount.tax_ids), (-39.0, self.vat_incl))
        self.assertAlmostEqual(order.amount_total, 2 * 195.0 - 39.0 + 4.95)
        self.assertEqual(order.state, "sale", "Discounted totals match exactly.")

    def test_match_by_sku_and_existing_customer(self):
        jane = self.env["res.partner"].create({"name": "Jane Doe (existing)", "email": "Jane@Example.com"})
        self.run_import(self.order_node(items=[(None, "SN-CDB-BLK", 1, 195.0, 0.0)]))
        order = self.find()
        self.assertEqual(order.partner_id, jane, "Matched by email, ignoring case.")
        self.assertEqual(jane.shopify_customer_id, "gid://shopify/Customer/7")
        self.assertIn(self.black, order.order_line.product_id, "Matched by SKU when the variant ID is unknown.")

    def test_shopify_error_keeps_position(self):
        self.store.orders_synced_until = datetime(2026, 11, 2, 8, 0)
        refused = FakeResponse({"errors": [{"message": "Access denied for orders field."}]})
        result, _calls = self.run_shopify(self.store._import_orders, refused)
        self.assertFalse(result)
        self.assertEqual(self.store.orders_synced_until, datetime(2026, 11, 2, 8, 0))
        log = self.env["shopify.sync.log"].search([("store_id", "=", self.store.id)], limit=1)
        self.assertEqual((log.operation, log.state), ("import_orders", "error"))

    def test_open_quotation_triggers_stock_update(self):
        self.store.stock_synced_until = datetime.now()  # no stock moved since
        self.run_import(self.order_node(status="PENDING"))
        # Records written in a test all carry the transaction's start time: move the order after it.
        self.env.flush_all()
        self.env.cr.execute("UPDATE sale_order SET write_date = now() + interval '1 minute' WHERE id = %s",
                            [self.find().id])
        self.env.invalidate_all()
        stock = gql({"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "x", "changes": [
            {"item": {"id": "gid://shopify/InventoryItem/1"}, "location": {"id": "gid://shopify/Location/1"}}]},
            "userErrors": []}})
        self.natural.shopify_inventory_item_id = "gid://shopify/InventoryItem/1"
        count, calls = self.run_shopify(self.store._sync_stock, stock)
        self.assertEqual(count, 1, "The variant on the new Shopify quotation is sent.")
        sent = self.body(calls[0])["variables"]["input"]["quantities"]
        self.assertEqual([(q["inventoryItemId"], q["quantity"]) for q in sent], [("gid://shopify/InventoryItem/1", 23)])

    # ------------------------------------------------------------------
    # Task 3.2: customers and addresses
    # ------------------------------------------------------------------
    @staticmethod
    def address(first="Jane", last="Doe", street="10 Downing Street", city="London", zip_code="SW1A 2AA",
                company=None, province=None, phone=None):
        return {"name": f"{first} {last}", "firstName": first, "lastName": last, "company": company,
                "address1": street, "address2": None, "city": city, "zip": zip_code, "province": province,
                "provinceCode": None, "countryCodeV2": "GB", "phone": phone}

    def gift_node(self, number):
        return self.order_node(number, billingAddress=self.address(), shippingAddress=self.address(
            "Margaret", "Doe", "1 Minster Yard", "York", "YO1 7HH", province="North Yorkshire"))

    def test_same_address_adds_no_contacts(self):
        self.run_import(self.order_node(billingAddress=self.address()))
        order = self.find()
        customer = order.partner_id
        self.assertEqual((order.partner_invoice_id, order.partner_shipping_id), (customer, customer))
        self.assertFalse(customer.child_ids)
        # Same place written differently: no new address either.
        self.run_import(self.order_node(1002, shippingAddress=self.address(zip_code="sw1a2aa", city="LONDON")))
        self.assertEqual(self.find(1002).partner_shipping_id, customer)
        self.assertFalse(customer.child_ids, "Capitals and spaces in the postcode do not make a new address.")

    def test_gift_goes_to_a_delivery_address(self):
        self.run_import(self.gift_node(1001))
        order = self.find()
        customer = order.partner_id
        self.assertEqual(customer.street, "10 Downing Street", "A new customer gets the billing address.")
        self.assertEqual(order.partner_invoice_id, customer)
        delivery = order.partner_shipping_id
        self.assertEqual((delivery.parent_id, delivery.type, delivery.name, delivery.city),
                         (customer, "delivery", "Margaret Doe", "York"))
        self.assertEqual(delivery.state_id.name, "North Yorkshire", "County matched by name.")
        self.assertEqual(order.picking_ids.partner_id, delivery, "The delivery order goes to the gift address.")

        self.run_import(self.gift_node(1002))
        self.assertEqual(self.find(1002).partner_shipping_id, delivery, "The same gift address is reused.")
        self.assertEqual(len(customer.child_ids), 1)

    def test_company_shows_on_the_delivery_address(self):
        self.run_import(self.order_node(billingAddress=self.address(), shippingAddress=self.address(
            street="1 Work Park", city="Leeds", zip_code="LS1 1AA", company="Acme Ltd")))
        order = self.find()
        self.assertEqual(order.partner_id.name, "Jane Doe")
        self.assertEqual(order.partner_id.company_type, "person")
        self.assertEqual(order.partner_shipping_id.name, "Jane Doe (Acme Ltd)")

    def test_existing_contact_is_not_overwritten(self):
        jane = self.env["res.partner"].create({
            "name": "Jane Doe (Saxby)", "email": "jane@example.com", "phone": "0113 000 0000",
            "street": "5 Park Row", "city": "Leeds", "zip": "LS1 5AB"})
        self.run_import(self.order_node(billingAddress=self.address()))
        order = self.find()
        self.assertEqual(order.partner_id, jane)
        self.assertEqual((jane.name, jane.phone, jane.street), ("Jane Doe (Saxby)", "0113 000 0000", "5 Park Row"),
                         "Odoo is the master: name, phone and address are kept.")
        self.assertEqual((order.partner_invoice_id.type, order.partner_invoice_id.street),
                         ("invoice", "10 Downing Street"))
        self.assertEqual((order.partner_shipping_id.type, order.partner_shipping_id.street),
                         ("delivery", "10 Downing Street"))

    def test_empty_contact_is_completed(self):
        jane = self.env["res.partner"].create({"name": "Existing Buyer", "email": "jane@example.com"})
        self.run_import(self.order_node())
        order = self.find()
        self.assertEqual(order.partner_id, jane)
        self.assertEqual((jane.street, jane.phone), ("10 Downing Street", "+447700900123"),
                         "Empty fields are filled from Shopify.")
        self.assertEqual(order.partner_shipping_id, jane, "Same place: no extra address despite another name.")

    def test_trade_contacts_are_not_used(self):
        Partner = self.env["res.partner"]
        trade = Partner.create({"name": "Doe Interiors", "is_company": True, "email": "jane@example.com"})
        Partner.create({"name": "Jane at Doe Interiors", "parent_id": trade.id, "email": "jane@example.com"})
        holdings = self.env["res.company"].create({"name": "Test Holdings"})
        Partner.create({"name": "Jane (Holdings)", "email": "jane@example.com", "company_id": holdings.id})
        self.run_import(self.order_node())
        customer = self.find().partner_id
        self.assertEqual(customer.name, "Jane Doe", "A private contact is created for the web order.")
        self.assertFalse(customer.parent_id or customer.company_id)

    def test_guest_without_email(self):
        self.run_import(self.order_node(customer=None, email=None,
                                        shippingAddress=self.address("Sam", "Guest", phone="+441130000000")))
        customer = self.find().partner_id
        self.assertEqual((customer.name, customer.phone, customer.email), ("Sam Guest", "+441130000000", False))
        self.assertFalse(customer.shopify_customer_id)

    def test_address_changed_before_shipping(self):
        self.run_import(self.order_node())
        order = self.find()
        self.run_import(self.gift_node(1001))
        self.assertEqual(order.partner_shipping_id.city, "York")
        self.assertEqual(order.picking_ids.partner_id, order.partner_shipping_id,
                         "The open delivery order follows the new address.")
        self.assertFalse(order.shopify_import_issue)

    def test_address_changed_after_shipping(self):
        self.run_import(self.order_node())
        order = self.find()
        customer = order.partner_id
        for move in order.picking_ids.move_ids:
            move.quantity = move.product_uom_qty
        order.picking_ids.move_ids.picked = True
        order.picking_ids._action_done()
        self.run_import(self.gift_node(1001))
        self.assertEqual(order.partner_shipping_id, customer, "A shipped order is not changed.")
        self.assertIn("after the order was shipped", order.shopify_import_issue)
        self.assertTrue(order.activity_ids)

    # ------------------------------------------------------------------
    # Task 3.3: shipping methods, invoices and payments
    # ------------------------------------------------------------------
    @staticmethod
    def tx(n, amount, kind="SALE", status="SUCCESS", when="2026-11-03T09:01:00Z", gateway="shopify_payments"):
        return {"id": f"gid://shopify/OrderTransaction/{n}", "kind": kind, "status": status, "gateway": gateway,
                "processedAt": when, "amountSet": {"shopMoney": {"amount": f"{amount:.2f}"}}}

    def invoicing_on(self):
        self.store.write({"create_invoices": True, "invoices_from": datetime(2026, 11, 1)})

    def payments(self, order):
        return self.env["account.payment"].search([("shopify_transaction_id", "!=", False),
                                                   ("memo", "like", order.shopify_order_name)])

    def test_paid_order_invoiced_and_paid(self):
        self.invoicing_on()
        node = self.order_node(transactions=[self.tx(1, 199.95)])
        self.run_import(node)
        order = self.find()
        invoice = order.invoice_ids
        self.assertEqual((len(invoice), invoice.state), (1, "posted"))
        self.assertAlmostEqual(invoice.amount_total, 199.95)
        self.assertEqual(str(invoice.invoice_date), "2026-11-03", "Invoice dated when Shopify took the money.")
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        payment = self.payments(order)
        self.assertEqual(payment.journal_id.code, "SHOP")
        self.assertEqual(payment.journal_id, self.store.payment_journal_id, "Shopify Payments created and kept.")
        self.assertEqual((payment.amount, payment.memo), (199.95, "#1001 shopify_payments"))
        self.assertEqual(payment.shopify_transaction_id, "gid://shopify/OrderTransaction/1")

        self.run_import(self.order_node(transactions=[self.tx(1, 199.95)], displayFulfillmentStatus="FULFILLED"))
        self.assertEqual(len(order.invoice_ids), 1, "Never invoiced twice.")
        self.assertEqual(len(self.payments(order)), 1, "Never paid twice.")

    def test_authorised_waits_for_capture(self):
        self.invoicing_on()
        auth = self.tx(1, 199.95, kind="AUTHORIZATION")
        self.run_import(self.order_node(status="AUTHORIZED", transactions=[auth]))
        order = self.find()
        self.assertEqual(order.state, "sale")
        self.assertFalse(order.invoice_ids, "Nothing taken yet: no invoice.")
        self.run_import(self.order_node(transactions=[auth, self.tx(2, 199.95, kind="CAPTURE",
                                                                     when="2026-11-05T10:00:00Z")]))
        self.assertEqual(str(order.invoice_ids.invoice_date), "2026-11-05")
        self.assertIn(order.invoice_ids.payment_state, ("paid", "in_payment"))
        self.assertEqual(self.payments(order).shopify_transaction_id, "gid://shopify/OrderTransaction/2")

    def test_partly_paid_then_paid(self):
        self.invoicing_on()
        first = self.tx(1, 50.0)
        self.run_import(self.order_node(status="PARTIALLY_PAID", transactions=[first]))
        order = self.find()
        self.assertEqual(order.invoice_ids.payment_state, "partial")
        self.assertAlmostEqual(order.invoice_ids.amount_residual, 149.95)
        self.run_import(self.order_node(transactions=[first, self.tx(2, 149.95, gateway="gift_card")]))
        self.assertIn(order.invoice_ids.payment_state, ("paid", "in_payment"))
        self.assertEqual(len(self.payments(order)), 2)
        self.assertEqual(len(order.invoice_ids), 1)

    def test_failed_transaction_is_ignored(self):
        self.invoicing_on()
        self.run_import(self.order_node(status="PENDING", transactions=[self.tx(1, 199.95, status="FAILURE")]))
        self.assertFalse(self.find().invoice_ids)

    def test_orders_before_invoicing_need_the_button(self):
        self.store.write({"create_invoices": True, "invoices_from": datetime(2026, 11, 4)})
        node = self.order_node(transactions=[self.tx(1, 199.95)])
        self.run_import(node)
        order = self.find()
        self.assertFalse(order.invoice_ids, "Orders placed before Invoice Orders From are left alone.")
        self.run_shopify(self.store.action_invoice_paid_orders, gql({"order": node}))
        self.assertEqual(order.invoice_ids.state, "posted")
        self.assertIn(order.invoice_ids.payment_state, ("paid", "in_payment"))

    def test_invoicing_off(self):
        self.store.write({"create_invoices": False, "invoices_from": datetime(2026, 11, 1)})
        self.run_import(self.order_node(transactions=[self.tx(1, 199.95)]))
        self.assertFalse(self.find().invoice_ids)
        self.store.create_invoices = True
        self.assertGreater(self.store.invoices_from, datetime(2026, 11, 3, 9, 0),
                           "Switching on starts now: orders from the off period need the button.")

    def test_shipping_rate_mapped_to_delivery_method(self):
        Map = self.env["shopify.shipping.map"]
        self.run_import(self.order_node())
        first = self.find()
        rate = Map.search([("store_id", "=", self.store.id), ("name", "=", "Standard")])
        self.assertEqual(len(rate), 1, "An unknown rate is added to the list.")
        self.assertFalse(first.carrier_id)
        self.assertEqual(first.order_line.filtered("is_delivery").product_id, self.store.order_shipping_product_id)

        product = self.env["product.product"].create({"name": "Royal Mail 48", "type": "service",
                                                      "taxes_id": [(6, 0, self.vat_incl.ids)]})
        carrier = self.env["delivery.carrier"].create({"name": "Royal Mail 48", "product_id": product.id,
                                                       "delivery_type": "fixed", "fixed_price": 9.99})
        rate.carrier_id = carrier
        self.run_import(self.order_node(1002))
        order = self.find(1002)
        line = order.order_line.filtered("is_delivery")
        self.assertEqual(order.carrier_id, carrier)
        self.assertEqual((line.product_id, line.price_unit, line.name), (product, 4.95, "Standard"),
                         "Shopify's price and name; Odoo never re-prices shipping.")
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.picking_ids.carrier_id, carrier, "The delivery order knows the method.")
        self.assertEqual(len(Map.search([("store_id", "=", self.store.id)])), 1)
