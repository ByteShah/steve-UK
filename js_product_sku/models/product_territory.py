from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductTerritory(models.Model):
    _name = "product.territory"
    _description = "Territory"
    _order = "code"
    _rec_names_search = ["code", "name"]

    code = fields.Char(required=True, size=2)
    name = fields.Char(required=True, translate=True)
    is_default = fields.Boolean(string="Default", help="Territory proposed on new products.")
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint("unique(code)", "Each territory code can only be used once.")
    _default_uniq = models.UniqueIndex("(is_default) WHERE is_default", "Only one territory can be the default.")

    @api.depends("code", "name")
    def _compute_display_name(self):
        for territory in self:
            territory.display_name = f"{territory.code} - {territory.name}"

    @api.constrains("code")
    def _check_code(self):
        for territory in self:
            if not (len(territory.code) == 2 and territory.code.isdigit()):
                raise ValidationError(_("The territory code must be exactly 2 digits, e.g. 12."))

    def write(self, vals):
        if "code" in vals:
            changed = self.filtered(lambda t: t.code != vals["code"])
            if changed and self.env["product.template"].with_context(active_test=False).search_count(
                [("territory_id", "in", changed.ids), ("unique_number", "!=", False)], limit=1
            ):
                raise ValidationError(_("This territory is used in product SKUs, so its code cannot be changed."))
        return super().write(vals)
