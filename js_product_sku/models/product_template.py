from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ProductTemplate(models.Model):
    _inherit = "product.template"

    territory_id = fields.Many2one(
        "product.territory", string="Territory", index=True, ondelete="restrict",
        default=lambda self: self.env["product.territory"].search([("is_default", "=", True)], limit=1),
        help="Territory this product version is made for: the first 2 digits of the SKU.")
    department_id = fields.Many2one(
        "product.category", string="Department", compute="_compute_department_id", store=True, index=True)
    unique_number = fields.Char(
        string="Unique Number", size=4, copy=False,
        help="Next number of the department, assigned when the product is saved.")
    sku_base = fields.Char(
        string="SKU Base", compute="_compute_sku_base", store=True, index=True,
        help="Territory + department + unique number: the first 7 digits of each variant SKU.")

    _unique_number_uniq = models.Constraint(
        "unique(territory_id, department_id, unique_number)",
        "This unique number is already used in this territory and department.")

    @api.depends("categ_id", "categ_id.parent_path")
    def _compute_department_id(self):
        departments = self.categ_id._get_departments()
        for template in self:
            template.department_id = departments.get(template.categ_id.id, False)

    @api.depends("territory_id.code", "department_id.department_code", "unique_number")
    def _compute_sku_base(self):
        for template in self:
            parts = (template.territory_id.code, template.department_id.department_code, template.unique_number)
            template.sku_base = "".join(parts) if all(parts) else False

    @api.onchange("unique_number")
    def _onchange_unique_number(self):
        if self.unique_number and self.unique_number.isdigit():
            self.unique_number = self.unique_number.zfill(4)

    @api.constrains("territory_id", "department_id", "unique_number")
    def _check_sku(self):
        for template in self.filtered("unique_number"):
            if not template.department_id:
                raise ValidationError(_(
                    "%s: the category must be under a department that has a department code.",
                    template.display_name))
            if not template.territory_id:
                raise ValidationError(_("%s: choose a territory for the SKU.", template.display_name))
            number = template.unique_number
            if not (len(number) == 4 and number.isdigit()):
                raise ValidationError(_("%s: the unique number must be 4 digits.", template.display_name))
        self.attribute_line_ids._check_sku_attribute()

    def _check_unique_number_access(self):
        if not self.env.su and not self.env.user.has_group("js_product_sku.group_sku_manager"):
            raise AccessError(_("Only SKU managers can set the unique number of a product."))

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("unique_number") for vals in vals_list):
            self._check_unique_number_access()
        default_categ_id = None
        Category = self.env["product.category"]
        categ_ids = []
        for vals in vals_list:
            if not vals.get("categ_id") and default_categ_id is None:
                default_categ_id = self.default_get(["categ_id"]).get("categ_id") or False
            categ_ids.append(vals.get("categ_id") or default_categ_id)
        departments = Category.browse([c for c in categ_ids if c])._get_departments()
        sku_vals = [(vals, departments[categ_id]) for vals, categ_id in zip(vals_list, categ_ids)
                    if categ_id in departments]
        if sku_vals:
            numbers = Category._reserve_unique_numbers([(dept, vals.get("unique_number")) for vals, dept in sku_vals])
            for (vals, _dept), number in zip(sku_vals, numbers):
                vals["unique_number"] = number
        return super().create(vals_list)

    def write(self, vals):
        if "unique_number" in vals:
            self._check_unique_number_access()
            if vals["unique_number"] and vals["unique_number"].isdigit():
                vals["unique_number"] = vals["unique_number"].zfill(4)
        if "territory_id" in vals and any(t.territory_id.id != vals["territory_id"] for t in self.filtered("unique_number")):
            raise ValidationError(_(
                "The territory of a product with an SKU cannot be changed. Create a new product for the other territory."))
        if vals.get("categ_id"):
            # The department digit of an existing SKU cannot change.
            new_department = self.env["product.category"].browse(vals["categ_id"])._get_departments().get(vals["categ_id"])
            if any(t.department_id != new_department for t in self.filtered("unique_number")):
                raise ValidationError(_(
                    "Products with an SKU can only be moved to a category of the same department."))
        res = super().write(vals)
        if {"unique_number", "categ_id"} & vals.keys():
            templates = self.filtered(lambda t: t.department_id and (not t.unique_number or "unique_number" in vals))
            if templates:
                numbers = self.env["product.category"]._reserve_unique_numbers(
                    [(t.department_id, t.unique_number) for t in templates])
                for template, number in zip(templates, numbers):
                    if template.unique_number != number:
                        # Automatic assignment, allowed for every user.
                        super(ProductTemplate, template).write({"unique_number": number})
        return res


class ProductTemplateAttributeLine(models.Model):
    _inherit = "product.template.attribute.line"

    @api.constrains("attribute_id", "product_tmpl_id")
    def _check_sku_attribute(self):
        # Any other variant attribute would give several variants the same SKU.
        for line in self.filtered(lambda l: l.product_tmpl_id.unique_number and l.attribute_id.create_variant != "no_variant"):
            if not line.attribute_id.sku_part:
                raise ValidationError(_(
                    "%(product)s has an SKU: its variants can only come from the Colour and Size attributes, "
                    "not from %(attribute)s. Set that attribute to never create variants.",
                    product=line.product_tmpl_id.display_name, attribute=line.attribute_id.name))
