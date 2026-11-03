"""Recompute product weights: products whose variants all weigh the same now show that weight."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].with_context(active_test=False).search([]).filtered(
        lambda t: len(t.product_variant_ids) > 1)
    env.add_to_compute(templates._fields["weight"], templates)
    templates.flush_recordset(["weight"])
