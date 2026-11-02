from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

METAFIELD_TYPES = [
    ("single_line_text_field", "Single line text"),
    ("multi_line_text_field", "Multi-line text"),
    ("rich_text_field", "Rich text"),
    ("number_integer", "Integer"),
    ("number_decimal", "Decimal"),
    ("boolean", "True or false"),
    ("dimension", "Dimension"),
    ("list.product_reference", "List of products"),
    ("json", "JSON"),
]


class ShopifyMetafieldMap(models.Model):
    _name = "shopify.metafield.map"
    _description = "Shopify Metafield Mapping"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    field_id = fields.Many2one(
        "ir.model.fields", string="Odoo Field", required=True, ondelete="cascade",
        domain="[('model', '=', 'product.template')]")
    field_name = fields.Char(related="field_id.name", string="Technical Name")
    namespace = fields.Char(required=True, default="custom")
    key = fields.Char(required=True)
    metafield_type = fields.Selection(METAFIELD_TYPES, string="Shopify Type", required=True)
    dimension_unit = fields.Selection(
        [("cm", "cm"), ("mm", "mm"), ("m", "m"), ("in", "in")], default="cm",
        help="Unit of the Odoo value, for Dimension metafields.")
    condition_field_id = fields.Many2one(
        "ir.model.fields", string="Only When", ondelete="set null",
        domain="[('model', '=', 'product.template'), ('ttype', '=', 'boolean')]",
        help="Boolean field that must be on for this metafield to be sent, e.g. Google Shopping.")
    note = fields.Char(help="For the Shopify partner: what this metafield shows.")

    _key_uniq = models.Constraint("unique(namespace, key)", "Each metafield namespace and key is mapped once.")

    @api.depends("namespace", "key")
    def _compute_display_name(self):
        for mapping in self:
            mapping.display_name = f"{mapping.namespace}.{mapping.key}"

    @api.constrains("namespace", "key")
    def _check_namespace_key(self):
        for mapping in self:
            for part in (mapping.namespace, mapping.key):
                if not (2 <= len(part) <= 64) or not all(c.isalnum() or c in "-_" for c in part):
                    raise ValidationError(_(
                        "Namespace and key use 2 to 64 letters, digits, '-' or '_' (%s).", part))
