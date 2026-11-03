"""Order import settings for stores created before order import existed (defaults only apply to new stores)."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    defaults = {
        "order_team_id": env.ref("js_shopify_connector.crm_team_shopify", raise_if_not_found=False),
        "order_shipping_product_id": env.ref("js_shopify_connector.product_shopify_shipping", raise_if_not_found=False),
        "order_discount_product_id": env.ref("js_shopify_connector.product_shopify_discount", raise_if_not_found=False),
    }
    for store in env["shopify.store"].with_context(active_test=False).search([]):
        store.write({field: record.id for field, record in defaults.items() if record and not store[field]})
        if not store.order_responsible_id:
            store.order_responsible_id = store.create_uid or env.ref("base.user_admin")
        if not store.auto_confirm_orders:
            store.auto_confirm_orders = True
