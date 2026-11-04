"""Customers and addresses for imported Shopify orders (Task 3.2).

Odoo is the master for customer data: a matched contact keeps its name and email, Shopify only fills
empty fields. Invoice and delivery addresses that differ from the customer's own address become child
contacts, reused whenever the same address comes again.
"""
import re

from odoo import _, models

# What makes two addresses "the same"; the name only counts for extra addresses (see _shopify_same_address).
ADDRESS_KEYS = ("name", "street", "street2", "city", "zip", "country_id")
ADDRESS_FIELDS = ("street", "street2", "city", "zip", "country_id", "state_id")


def _norm(value):
    """Compare ignoring capitals and spaces, so 'LS11AA' equals 'LS1 1AA'."""
    return re.sub(r"\s+", "", str(value or "")).casefold()


class ShopifyStore(models.Model):
    _inherit = "shopify.store"

    # ------------------------------------------------------------------
    # Customer
    # ------------------------------------------------------------------
    def _shopify_partner_domain(self):
        """Contacts a web order may use: private people of their own, shared or of the store company.
        Trade companies, people inside a company and other companies' contacts are left alone."""
        return [("parent_id", "=", False), ("is_company", "=", False),
                ("company_id", "in", [False, self.company_id.id])]

    def _shopify_find_partner(self, node):
        """Contact by Shopify customer ID, then by email; otherwise a new individual contact."""
        Partner = self.env["res.partner"].sudo()
        customer = node.get("customer") or {}
        email = (customer.get("email") or node.get("email") or "").strip()
        address = (self._shopify_address_values(node.get("billingAddress"))
                   or self._shopify_address_values(node.get("shippingAddress")))
        phone = customer.get("phone") or node.get("phone") or address.get("phone") or False

        partner = Partner.browse()
        if customer.get("id"):
            partner = Partner.search([("shopify_customer_id", "=", customer["id"])], limit=1)
        if not partner and email:
            partner = Partner.search([("email", "=ilike", email)] + self._shopify_partner_domain(),
                                     limit=1, order="id")
        if partner:
            vals = {}
            if customer.get("id") and not partner.shopify_customer_id:
                vals["shopify_customer_id"] = customer["id"]
            if phone and not partner.phone:
                vals["phone"] = phone
            if address and not (partner.street or partner.city or partner.zip):
                vals.update({key: address[key] for key in ADDRESS_FIELDS})
            if vals:
                partner.write(vals)
            return partner

        person = " ".join(filter(None, [customer.get("firstName"), customer.get("lastName")])).strip()
        return Partner.create({
            "name": person or address.get("person") or address.get("name") or email
            or _("Shopify customer %s", node["name"]),
            "company_type": "person",
            "email": email or False,
            "phone": phone,
            "shopify_customer_id": customer.get("id") or False,
            **{key: address[key] for key in ADDRESS_FIELDS if address},
        })

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------
    def _shopify_address_values(self, address):
        """Shopify MailingAddress -> res.partner values ({} when there is no real address)."""
        if not address or not any(address.get(key) for key in ("address1", "city", "zip")):
            return {}
        Country, State = self.env["res.country"], self.env["res.country.state"]
        country = Country.search([("code", "=", address["countryCodeV2"])], limit=1) \
            if address.get("countryCodeV2") else Country
        state = State
        if country and address.get("provinceCode"):
            state = State.search([("country_id", "=", country.id), ("code", "=", address["provinceCode"])], limit=1)
        if country and not state and address.get("province"):
            state = State.search([("country_id", "=", country.id), ("name", "=ilike", address["province"])], limit=1)
        person = " ".join(filter(None, [address.get("firstName"), address.get("lastName")])).strip() \
            or (address.get("name") or "").strip()
        company = (address.get("company") or "").strip()
        return {
            "person": person,
            # The company shows on the delivery label; the customer stays an individual.
            "name": f"{person} ({company})" if person and company else person or company,
            "street": address.get("address1") or False,
            "street2": address.get("address2") or False,
            "city": address.get("city") or False,
            "zip": address.get("zip") or False,
            "country_id": country.id or False,
            "state_id": state.id or False,
            "phone": address.get("phone") or False,
        }

    @staticmethod
    def _shopify_same_address(partner, values):
        """Same place; for an extra address also the same recipient. The customer's own contact is
        compared without the name, so 'Jane Doe' on Shopify still matches 'Jane Doe (Saxby)' in Odoo."""
        def value(key):
            return partner[key].id if key == "country_id" else partner[key]
        same_place = all(_norm(value(key)) == _norm(values.get(key)) for key in ADDRESS_KEYS if key != "name")
        return same_place and (not partner.parent_id or not values.get("name")
                               or _norm(partner.name) == _norm(values["name"]))

    def _shopify_order_address(self, partner, values, kind):
        """The customer itself, an existing child address of this kind, or a new one."""
        if not values or self._shopify_same_address(partner, values):
            return partner
        for child in partner.child_ids.filtered(lambda c: c.type == kind):
            if self._shopify_same_address(child, values):
                return child
        return self.env["res.partner"].sudo().create({
            "parent_id": partner.id,
            "type": kind,
            "name": values["name"] or partner.name,
            "phone": values["phone"],
            **{key: values[key] for key in ADDRESS_FIELDS},
        })

    def _shopify_address_order_values(self, partner, node):
        return {
            "partner_id": partner.id,
            "partner_invoice_id": self._shopify_order_address(
                partner, self._shopify_address_values(node.get("billingAddress")), "invoice").id,
            "partner_shipping_id": self._shopify_order_address(
                partner, self._shopify_address_values(node.get("shippingAddress")), "delivery").id,
        }

    def _shopify_sync_delivery_address(self, order, node):
        """Follow a shipping address edited in Shopify while nothing has shipped; warn afterwards."""
        values = self._shopify_address_values(node.get("shippingAddress"))
        if not values or self._shopify_same_address(order.partner_shipping_id, values):
            return
        if order.picking_ids.filtered(lambda p: p.state == "done"):
            self._shopify_add_issue(order, _(
                "The shipping address was changed in Shopify after the order was shipped: %s",
                ", ".join(filter(None, [values["name"], values["street"], values["city"], values["zip"]]))))
            return
        address = self._shopify_order_address(order.partner_id, values, "delivery")
        # The context also moves open delivery orders to the new address.
        order.with_context(update_delivery_shipping_partner=True).write({"partner_shipping_id": address.id})
        order.message_post(body=_("Delivery address changed in Shopify to %s.", address.display_name))
