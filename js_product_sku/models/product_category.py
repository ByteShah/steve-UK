from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

MAX_UNIQUE_NUMBER = 9999


class ProductCategory(models.Model):
    _inherit = "product.category"

    department_code = fields.Char(
        string="Department Code", size=1, copy=False,
        help="One digit used in the SKU. Set it on top-level categories only.")
    next_unique_number = fields.Integer(
        string="Next Unique Number", default=1, copy=False,
        help="Unique number proposed for the next new product of this department.")

    _department_code_uniq = models.Constraint(
        "unique(department_code)", "Each department code can only be used once.")

    @api.constrains("department_code", "parent_id")
    def _check_department_code(self):
        for categ in self.filtered("department_code"):
            if categ.parent_id:
                raise ValidationError(_("The department code can only be set on a top-level category."))
            if not categ.department_code.isdigit():
                raise ValidationError(_("The department code must be a single digit (0-9)."))

    @api.constrains("next_unique_number")
    def _check_next_unique_number(self):
        for categ in self:
            if not 0 <= categ.next_unique_number <= MAX_UNIQUE_NUMBER + 1:
                raise ValidationError(_("The next unique number must be between 0 and %s.", MAX_UNIQUE_NUMBER))

    def write(self, vals):
        # Moving a category or changing its code would change existing SKUs and barcodes.
        if "parent_id" in vals or "department_code" in vals:
            changed = self.filtered(
                lambda c: c.parent_id.id != vals.get("parent_id", c.parent_id.id)
                or c.department_code != vals.get("department_code", c.department_code)
            )
            if changed and self.env["product.template"].with_context(active_test=False).search_count(
                [("categ_id", "child_of", changed.ids), ("unique_number", "!=", False)], limit=1
            ):
                raise ValidationError(_(
                    "Products with an SKU use this category, so it cannot be moved "
                    "and its department code cannot be changed."))
        res = super().write(vals)
        if "department_code" in vals:
            # department_id only depends on the product's own category
            templates = self.env["product.template"].with_context(active_test=False).search(
                [("categ_id", "child_of", self.ids)])
            self.env.add_to_compute(templates._fields["department_id"], templates)
        return res

    def _get_departments(self):
        """Return {category id: department}, the department being the top-level
        category when it has a department code."""
        root_ids = {categ.id: int(categ.parent_path.split("/")[0]) for categ in self if categ.parent_path}
        departments = self.browse(set(root_ids.values())).filtered("department_code")
        return {categ_id: departments.browse(root_id) for categ_id, root_id in root_ids.items()
                if root_id in departments._ids}

    @api.model
    def _reserve_unique_numbers(self, rows):
        """`rows` is a list of (department, unique number or False). Returns the numbers,
        empty ones replaced by the department's next free number. Counters move past
        every number used."""
        departments = self.browse({dept.id for dept, _number in rows}).sudo()
        self.env.cr.execute("SELECT id FROM product_category WHERE id IN %s FOR UPDATE", [departments._ids])
        departments.invalidate_recordset(["next_unique_number"])
        next_numbers = {dept.id: dept.next_unique_number for dept in departments}

        rows = [(dept.id, number.zfill(4) if number and number.isdigit() else number) for dept, number in rows]
        for dept_id, number in rows:
            if number and number.isdigit():
                next_numbers[dept_id] = max(next_numbers[dept_id], int(number) + 1)

        result = []
        for dept_id, number in rows:
            if not number:
                number = f"{next_numbers[dept_id]:04d}"
                next_numbers[dept_id] += 1
            result.append(number)

        for dept in departments:
            if next_numbers[dept.id] > MAX_UNIQUE_NUMBER + 1:
                raise UserError(_("Department %s has used all its unique numbers.", dept.display_name))
            if next_numbers[dept.id] != dept.next_unique_number:
                dept.next_unique_number = next_numbers[dept.id]
        return result
