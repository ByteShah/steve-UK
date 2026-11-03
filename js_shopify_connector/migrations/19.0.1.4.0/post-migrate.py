"""Align the default metafield mappings with the Shopify partner's definitions.

The mapping data is noupdate, so existing records keep their old keys on upgrade; new
records (split GPSR contacts, booleans) are created by the data file afterwards.
"""
from odoo import SUPERUSER_ID, api

UPDATES = {
    "metafield_width": {"namespace": "custom", "key": "width", "metafield_type": "number_decimal"},
    "metafield_depth": {"namespace": "custom", "key": "depth", "metafield_type": "number_decimal"},
    "metafield_height": {"namespace": "custom", "key": "height", "metafield_type": "number_decimal"},
    "metafield_care_instructions": {"namespace": "custom", "key": "care_instructions", "metafield_type": "multi_line_text_field"},
    "metafield_age_safety": {"namespace": "custom", "key": "age_suitability", "metafield_type": "single_line_text_field"},
    "metafield_gpsr_manufacturer": {"namespace": "custom", "key": "gpsr_manufacturer_name", "metafield_type": "single_line_text_field", "partner_part": "name"},
    "metafield_gpsr_eu_responsible": {"namespace": "custom", "key": "gpsr_eu_responsible_person_name", "metafield_type": "single_line_text_field", "partner_part": "name"},
    "metafield_lead_time": {"namespace": "custom", "key": "sale_delay", "metafield_type": "number_integer"},
    "metafield_trade_note": {"namespace": "custom", "key": "product_trade_note", "metafield_type": "multi_line_text_field"},
    "metafield_google_age_group": {"namespace": "custom", "key": "google_age_group", "metafield_type": "single_line_text_field"},
    "metafield_google_gender": {"namespace": "custom", "key": "google_gender", "metafield_type": "single_line_text_field"},
    "metafield_google_condition": {"namespace": "custom", "key": "google_condition", "metafield_type": "single_line_text_field"},
    "metafield_google_mpn": {"namespace": "custom", "key": "google_mpn", "metafield_type": "single_line_text_field"},
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid, vals in UPDATES.items():
        mapping = env.ref(f"js_shopify_connector.{xmlid}", raise_if_not_found=False)
        if mapping:
            mapping.write(vals)
