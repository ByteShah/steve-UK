"""Automatic invoicing for stores created before Task 3.3: on, starting now, so orders that were
already imported are only invoiced through Invoice Paid Orders."""
from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for store in env["shopify.store"].with_context(active_test=False).search([]):
        store.write({"create_invoices": True, "invoices_from": fields.Datetime.now()})
