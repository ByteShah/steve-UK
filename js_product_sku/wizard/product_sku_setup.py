import re

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

# Product references from before the module: D UUU CCC SS, e.g. 300100302
OLD_REFERENCE = re.compile(r"^([0-9])([0-9]{3})([0-9]{3})([0-9]{2})$")


class ProductSkuSetup(models.TransientModel):
    _name = "product.sku.setup"
    _description = "SKU Setup"

    territory_id = fields.Many2one(
        "product.territory", string="Territory", required=True,
        default=lambda self: self.env["product.territory"].search([("is_default", "=", True)], limit=1),
        help="Set on every product that has no territory yet.")
    colour_attribute_id = fields.Many2one(
        "product.attribute", string="Colour Attribute", domain=[("create_variant", "!=", "no_variant")])
    size_attribute_id = fields.Many2one(
        "product.attribute", string="Size Attribute", domain=[("create_variant", "!=", "no_variant")])
    use_old_references = fields.Boolean(
        string="Keep Old Numbers", default=True,
        help="Take each product's unique number from its old 9-digit reference (D UUU CCC SS). "
             "Products without one get the next number of their department.")
    department_line_ids = fields.One2many("product.sku.setup.department", "wizard_id", string="Departments")
    value_line_ids = fields.One2many("product.sku.setup.value", "wizard_id", string="Colour & Size Codes")
    product_count = fields.Integer(string="Products to Number", compute="_compute_product_count")

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        colour = self._find_attribute("colour", ["colour", "color"])
        size = self._find_attribute("size", ["size"])
        dept_codes, value_codes = self._guess_codes(colour, size)
        res.update({
            "colour_attribute_id": colour.id,
            "size_attribute_id": size.id,
            "department_line_ids": [
                Command.create({"category_id": dept.id, "department_code": dept.department_code or dept_codes.get(dept.id)})
                for dept in self.env["product.category"].search([("parent_id", "=", False)])
            ],
            "value_line_ids": self._value_line_commands(colour, size, value_codes),
        })
        return res

    @api.onchange("colour_attribute_id", "size_attribute_id")
    def _onchange_attributes(self):
        _dept_codes, value_codes = self._guess_codes(self.colour_attribute_id, self.size_attribute_id)
        self.value_line_ids = [Command.clear()] + self._value_line_commands(
            self.colour_attribute_id, self.size_attribute_id, value_codes)

    @api.depends("department_line_ids.department_code")
    def _compute_product_count(self):
        Template = self.env["product.template"].with_context(active_test=False)
        for wizard in self:
            categs = wizard.department_line_ids.filtered("department_code").category_id
            wizard.product_count = Template.search_count(
                [("categ_id", "child_of", categs.ids), ("unique_number", "=", False)]) if categs else 0

    @api.model
    def _find_attribute(self, sku_part, names):
        Attribute = self.env["product.attribute"]
        attribute = Attribute.search([("sku_part", "=", sku_part)], limit=1)
        if not attribute:
            candidates = Attribute.search([("name", "in", [n.capitalize() for n in names] + names),
                                           ("create_variant", "!=", "no_variant")])
            attribute = candidates.sorted(lambda a: len(a.attribute_line_ids), reverse=True)[:1]
        return attribute

    @api.model
    def _guess_codes(self, colour, size):
        """Read department, colour and size codes from the old references.
        Returns ({top category id: code}, {attribute value id: code}); codes
        found with more than one value are left out."""
        dept_found, value_found = {}, {}
        products = self.env["product.product"].with_context(active_test=False).search(
            [("default_code", "=like", "_" * 9)])
        for product in products:
            match = OLD_REFERENCE.match(product.default_code)
            if not (match and product.categ_id.parent_path):
                continue
            dept, _number, colour_code, size_code = match.groups()
            dept_found.setdefault(int(product.categ_id.parent_path.split("/")[0]), set()).add(dept)
            for ptav in product.product_template_attribute_value_ids:
                code = {colour: colour_code, size: size_code}.get(ptav.attribute_id)
                if code:
                    value_found.setdefault(ptav.product_attribute_value_id.id, set()).add(code)
        return ({key: codes.pop() for key, codes in dept_found.items() if len(codes) == 1},
                {key: codes.pop() for key, codes in value_found.items() if len(codes) == 1})

    @api.model
    def _value_line_commands(self, colour, size, value_codes):
        return [
            Command.create({"value_id": value.id, "sku_code": value.sku_code or value_codes.get(value.id)})
            for value in (colour | size).value_ids
        ]

    def action_run(self):
        self.ensure_one()
        Template = self.env["product.template"].with_context(active_test=False)
        Attribute = self.env["product.attribute"]
        if self.colour_attribute_id and self.colour_attribute_id == self.size_attribute_id:
            raise UserError(_("Colour and size need two different attributes."))

        for line in self.department_line_ids:
            if line.category_id.department_code != (line.department_code or False):
                line.category_id.department_code = line.department_code or False

        # Codes first: setting the SKU part checks that every value has one
        for line in self.value_line_ids:
            if line.value_id.sku_code != line.sku_code:
                line.value_id.sku_code = line.sku_code
        for attribute, part in ((self.colour_attribute_id, "colour"), (self.size_attribute_id, "size")):
            if attribute.sku_part != part:
                (Attribute.search([("sku_part", "=", part)]) - attribute).sku_part = False
                attribute.sku_part = part

        Template.search([("territory_id", "=", False)]).territory_id = self.territory_id

        templates = Template.search([("department_id", "!=", False), ("unique_number", "=", False)])
        numbers, errors = {}, []
        if self.use_old_references:
            for template in templates:
                matches = [OLD_REFERENCE.match(code or "") for code in template.product_variant_ids.mapped("default_code")]
                found = {(m.group(1), m.group(2)) for m in matches if m}
                if len(found) > 1 or (found and next(iter(found))[0] != template.department_id.department_code):
                    errors.append(_("%(product)s: %(references)s", product=template.display_name,
                                    references=", ".join(template.product_variant_ids.mapped("default_code"))))
                elif found:
                    numbers[template] = next(iter(found))[1].zfill(4)
        if errors:
            raise UserError(_(
                "These products have old references that don't match one department and number. "
                "Fix their references or untick Keep Old Numbers:\n%s", "\n".join(errors)))

        # Old numbers first, so the new ones continue after the highest of them
        for template, number in numbers.items():
            template.unique_number = number
        for template in templates.filtered(lambda t: t not in numbers):
            template.unique_number = False  # assigns the department's next number

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("%(count)s products got an SKU (%(kept)s kept their old number).",
                             count=len(templates), kept=len(numbers)),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }


class ProductSkuSetupDepartment(models.TransientModel):
    _name = "product.sku.setup.department"
    _description = "SKU Setup Department"

    wizard_id = fields.Many2one("product.sku.setup", required=True, ondelete="cascade")
    category_id = fields.Many2one("product.category", string="Top-level Category", required=True)
    department_code = fields.Char(string="Department Code", size=1)


class ProductSkuSetupValue(models.TransientModel):
    _name = "product.sku.setup.value"
    _description = "SKU Setup Attribute Value"

    wizard_id = fields.Many2one("product.sku.setup", required=True, ondelete="cascade")
    value_id = fields.Many2one("product.attribute.value", string="Value", required=True)
    attribute_id = fields.Many2one(related="value_id.attribute_id")
    sku_code = fields.Char(string="SKU Code")
