import logging
import re
import time

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services.shopify_client import ShopifyClient, ShopifyError

_logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2026-10"

SHOP_QUERY = """
query { shop { name myshopifyDomain currencyCode } }
"""

LOCATIONS_QUERY = """
query ($after: String) {
  locations(first: 50, after: $after, includeInactive: true) {
    nodes { id name isActive address { countryCode } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PUBLICATIONS_QUERY = """
query ($after: String) {
  publications(first: 50, after: $after) {
    nodes { id catalog { title } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Older API versions name publications directly.
PUBLICATIONS_QUERY_LEGACY = """
query ($after: String) {
  publications(first: 50, after: $after) {
    nodes { id name }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ShopifyStore(models.Model):
    _name = "shopify.store"
    _description = "Shopify Store"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        help="Company whose prices, costs and stock are sent to Shopify.")
    shop_domain = fields.Char(
        string="Shop Domain", required=True, help="The store's myshopify domain, e.g. saxby-nash.myshopify.com")
    api_version = fields.Char(
        string="API Version", required=True, default=DEFAULT_API_VERSION,
        help="Shopify Admin API version, e.g. 2026-10. Shopify supports each version for at least 12 months.")
    auth_method = fields.Selection(
        [("client_credentials", "App client ID and secret"), ("access_token", "Admin API access token")],
        string="Connect With", required=True, default="client_credentials",
        help="Apps made in the Shopify Dev Dashboard use their client ID and secret. "
             "Older custom apps made in the Shopify admin give a fixed Admin API access token.")
    client_id = fields.Char(string="Client ID", groups="js_shopify_connector.group_shopify_manager")
    client_secret = fields.Char(string="Client Secret", groups="js_shopify_connector.group_shopify_manager")
    access_token = fields.Char(string="Access Token", copy=False, groups="js_shopify_connector.group_shopify_manager")
    access_token_expiry = fields.Datetime(
        string="Token Expiry", copy=False, readonly=True, groups="js_shopify_connector.group_shopify_manager")

    state = fields.Selection(
        [("draft", "Not Connected"), ("connected", "Connected"), ("error", "Error")],
        default="draft", required=True, readonly=True, copy=False)
    shop_name = fields.Char(readonly=True, copy=False)
    shop_currency = fields.Char(string="Shop Currency", readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)

    vendor = fields.Char(
        default=lambda self: self.env.company.name,
        help="Brand name sent as the vendor of every product.")
    location_ids = fields.One2many("shopify.location", "store_id", string="Locations")
    publication_ids = fields.One2many("shopify.publication", "store_id", string="Sales Channels")
    log_count = fields.Integer(compute="_compute_log_count")
    stock_synced_until = fields.Datetime(
        string="Stock Sent Until", readonly=True, copy=False,
        help="Stock changes up to this time have been sent to Shopify.")

    _one_active_store = models.UniqueIndex(
        "(active) WHERE active", "Only one Shopify store can be active: products keep one set of Shopify IDs.")

    def _compute_log_count(self):
        counts = dict(self.env["shopify.sync.log"]._read_group(
            [("store_id", "in", self.ids)], ["store_id"], ["__count"]))
        for store in self:
            store.log_count = counts.get(store, 0)

    @api.model
    def _normalize_domain(self, domain):
        domain = (domain or "").strip().lower()
        domain = re.sub(r"^https?://", "", domain).split("/")[0]
        if domain and "." not in domain:
            domain = f"{domain}.myshopify.com"
        return domain

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("shop_domain"):
                vals["shop_domain"] = self._normalize_domain(vals["shop_domain"])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("shop_domain"):
            vals["shop_domain"] = self._normalize_domain(vals["shop_domain"])
        if {"shop_domain", "auth_method", "client_id", "client_secret"} & vals.keys() and "access_token" not in vals:
            # A cached app token belongs to the old credentials.
            if vals.get("auth_method", "client_credentials") == "client_credentials":
                vals.update(access_token=False, access_token_expiry=False)
            vals["state"] = "draft"
        return super().write(vals)

    @api.constrains("shop_domain")
    def _check_shop_domain(self):
        for store in self:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", store.shop_domain or ""):
                raise ValidationError(_("Use the store's myshopify domain, e.g. saxby-nash.myshopify.com"))

    @api.constrains("api_version")
    def _check_api_version(self):
        for store in self:
            if not re.fullmatch(r"\d{4}-(01|04|07|10)|unstable", store.api_version or ""):
                raise ValidationError(_("The API version looks like 2026-10 (year and quarter month)."))

    @api.model
    def _get_active_store(self):
        return self.search([], limit=1)

    def _client(self):
        return ShopifyClient(self)

    # ------------------------------------------------------------------
    # Calls with logging
    # ------------------------------------------------------------------
    def _call(self, operation, query, variables=None, product=False, mutation=None):
        """Run a GraphQL call and log it. Raises ShopifyError after logging the failure.
        With `mutation`, its userErrors count as a failure and its result node is returned."""
        self.ensure_one()
        started = time.monotonic()
        try:
            data = self._client().graphql(query, variables)
            if mutation:
                data = ShopifyClient.check_user_errors(data, mutation)
        except ShopifyError as error:
            self.env["shopify.sync.log"]._log(
                self, operation, "error", str(error), product=product, request=variables,
                response=error.response, duration=time.monotonic() - started)
            raise
        self.env["shopify.sync.log"]._log(
            self, operation, "success", product=product, request=variables, duration=time.monotonic() - started)
        return data

    def _paginate(self, operation, query, connection):
        after, nodes = None, []
        while True:
            data = self._call(operation, query, {"after": after})[connection]
            nodes += data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                return nodes
            after = data["pageInfo"]["endCursor"]

    def _notify(self, message, kind="success"):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": kind, "message": message, "sticky": kind == "danger",
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    def _fail(self, error):
        self.write({"state": "error", "last_error": str(error)})
        return self._notify(_("Shopify: %s", error), "danger")

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            shop = self._call("test_connection", SHOP_QUERY)["shop"]
        except ShopifyError as error:
            return self._fail(error)
        self.write({"state": "connected", "last_error": False,
                    "shop_name": shop["name"], "shop_currency": shop["currencyCode"]})
        if shop["currencyCode"] != self.company_id.currency_id.name:
            return self._notify(_(
                "Connected to %(shop)s, but the shop currency is %(shop_currency)s and the company currency is "
                "%(currency)s. Prices are sent as they are in Odoo.",
                shop=shop["name"], shop_currency=shop["currencyCode"], currency=self.company_id.currency_id.name),
                "warning")
        return self._notify(_("Connected to %s.", shop["name"]))

    def action_fetch_locations(self):
        self.ensure_one()
        try:
            nodes = self._paginate("fetch_locations", LOCATIONS_QUERY, "locations")
        except ShopifyError as error:
            return self._fail(error)
        Location = self.env["shopify.location"].with_context(active_test=False)
        existing = {loc.shopify_id: loc for loc in Location.search([("store_id", "=", self.id)])}
        for node in nodes:
            vals = {"name": node["name"], "active": node["isActive"],
                    "country_code": (node.get("address") or {}).get("countryCode")}
            if node["id"] in existing:
                existing.pop(node["id"]).write(vals)
            else:
                Location.create(dict(vals, store_id=self.id, shopify_id=node["id"]))
        for gone in existing.values():
            gone.active = False
        linked = self._auto_link_warehouse()
        if linked:
            return self._notify(_("%(count)s Shopify location(s) loaded. Warehouse %(warehouse)s now sends its "
                                  "stock to %(location)s.", count=len(nodes), warehouse=linked.name,
                                  location=linked.shopify_location_id.name))
        if not self._get_stock_warehouses():
            return self._notify(_("%s Shopify location(s) loaded. Link a warehouse to a location (Inventory › "
                                  "Configuration › Warehouses) so stock is sent to Shopify.", len(nodes)), "warning")
        return self._notify(_("%s Shopify location(s) loaded.", len(nodes)))

    def action_fetch_publications(self):
        self.ensure_one()
        try:
            try:
                nodes = self._paginate("fetch_publications", PUBLICATIONS_QUERY, "publications")
                names = {n["id"]: (n.get("catalog") or {}).get("title") for n in nodes}
            except ShopifyError:
                nodes = self._paginate("fetch_publications", PUBLICATIONS_QUERY_LEGACY, "publications")
                names = {n["id"]: n.get("name") for n in nodes}
        except ShopifyError as error:
            return self._fail(error)
        Publication = self.env["shopify.publication"].with_context(active_test=False)
        existing = {pub.shopify_id: pub for pub in Publication.search([("store_id", "=", self.id)])}
        for shopify_id, name in names.items():
            name = name or shopify_id
            if shopify_id in existing:
                existing.pop(shopify_id).write({"name": name, "active": True})
            else:
                Publication.create({"store_id": self.id, "shopify_id": shopify_id, "name": name,
                                    "publish_products": name == "Online Store"})
        for gone in existing.values():
            gone.active = False
        return self._notify(_("%s Shopify sales channel(s) loaded.", len(names)))

    # ------------------------------------------------------------------
    # Ongoing sync
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_products(self):
        store = self._get_active_store()
        if store:
            store._sync_changed_products(commit=True)

    @api.model
    def _cron_sync_stock(self):
        store = self._get_active_store()
        if store:
            store._sync_stock(commit=True)

    def _sync_changed_products(self, commit=False):
        """Update every published product marked To Sync. Returns (done, failed)."""
        self.ensure_one()
        templates = self.env["product.template"].with_context(active_test=False).search(
            [("shopify_product_id", "!=", False), ("shopify_sync_state", "=", "to_sync")], order="write_date")
        done = failed = 0
        for position, template in enumerate(templates, start=1):
            try:
                with self.env.cr.savepoint():
                    ok = template._shopify_sync(self)
            except Exception as error:  # noqa: BLE001 - one broken product must not stop the others
                _logger.exception("Shopify sync of %s failed", template.display_name)
                template._shopify_set_error(_("Unexpected error: %s", error))
                self.env["shopify.sync.log"]._log(self, "sync_product", "error", str(error), product=template)
                ok = False
            done += ok
            failed += not ok
            if commit and self.env["ir.cron"]._commit_progress(1, remaining=len(templates) - position) <= 0:
                break
        return done, failed

    def _get_stock_warehouses(self):
        """Warehouses whose stock is sent to this store: those linked to one of its active locations."""
        self.ensure_one()
        return self.env["stock.warehouse"].sudo().search([
            ("shopify_location_id.store_id", "=", self.id), ("shopify_location_id.active", "=", True),
            ("company_id", "=", self.company_id.id)])

    def _shopify_resend_all_stock(self):
        """A warehouse link changed: the next stock run sends every variant, not only the ones
        whose stock moved, so stock that was already there reaches the new location."""
        self.sudo().write({"stock_synced_until": False})

    def _auto_link_warehouse(self):
        """Link the company's only warehouse to the store's only active location, when neither
        choice is ambiguous and nothing is linked yet."""
        self.ensure_one()
        locations = self.location_ids.filtered("active")
        warehouses = self.env["stock.warehouse"].sudo().search([("company_id", "=", self.company_id.id)])
        if len(locations) == 1 and len(warehouses) == 1 and not self._get_stock_warehouses():
            warehouses.shopify_location_id = locations
            return warehouses
        return self.env["stock.warehouse"]

    def _sync_stock(self, commit=False, full=False):
        """Send the stock of variants whose stock moved since the last run (all of them with `full`).
        Returns the number of variants sent, False when Shopify refused, None when no warehouse is linked."""
        self.ensure_one()
        if not self._get_stock_warehouses():
            # Nothing to send to; keep the position so the stock goes once a warehouse is linked.
            return None
        started = fields.Datetime.now()
        Variant = self.env["product.product"]
        domain = [("shopify_inventory_item_id", "!=", False), ("is_storable", "=", True),
                  ("product_tmpl_id.shopify_enabled", "=", True)]
        since = self.stock_synced_until
        if since and not full:
            moved = {product.id for [product] in self.env["stock.move"]._read_group(
                [("write_date", ">=", since)], ["product_id"])}
            moved |= {product.id for [product] in self.env["stock.quant"]._read_group(
                [("write_date", ">=", since)], ["product_id"])}
            domain.append(("id", "in", list(moved)))
        variants = Variant.search(domain)
        try:
            variants._shopify_push_stock(self)
        except ShopifyError:
            # Logged by _call; the same changes are sent again on the next run.
            return False
        self.stock_synced_until = started
        if commit:
            self.env["ir.cron"]._commit_progress(len(variants))
        return len(variants)

    def action_sync_products_now(self):
        self.ensure_one()
        done, failed = self._sync_changed_products()
        if failed:
            return self._notify(_("%(done)s product(s) updated in Shopify, %(failed)s failed.",
                                  done=done, failed=failed), "warning")
        return self._notify(_("%s product(s) updated in Shopify.", done))

    def action_sync_stock_now(self):
        self.ensure_one()
        count = self._sync_stock(full=True)
        if count is None:
            return self._notify(_(
                "No warehouse is linked to a Shopify location, so no stock was sent. Open Inventory › "
                "Configuration › Warehouses and set Shopify Location on the warehouse."), "warning")
        if count is False:
            return self._notify(_("Shopify refused the stock update. See the sync log."), "danger")
        return self._notify(_("Stock of %s variant(s) sent to Shopify.", count))

    def action_view_logs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sync Log"),
            "res_model": "shopify.sync.log",
            "view_mode": "list,form",
            "domain": [("store_id", "=", self.id)],
        }
