"""Refunds for stores created before Task 3.5: followed from now on, so refunds made earlier
(e.g. test refunds) do not create credit notes or returns."""
from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for store in env["shopify.store"].with_context(active_test=False).search([]):
        store.write({"process_refunds": True, "refunds_from": fields.Datetime.now()})
