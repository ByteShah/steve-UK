from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestProductSku(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.territory = cls.env.ref("js_product_sku.territory_uk")
        (cls.env["product.territory"].search([]) - cls.territory).is_default = False
        cls.territory.is_default = True
        cls.territory_eu = cls.env["product.territory"].create({"code": "02", "name": "Europe"})
        cls.pets = cls.env["product.category"].create({"name": "Pets", "department_code": "3"})
        cls.home = cls.env["product.category"].create({"name": "Home", "department_code": "1"})
        cls.harnesses = cls.env["product.category"].create({
            "name": "Harnesses",
            "parent_id": cls.env["product.category"].create({"name": "Dog", "parent_id": cls.pets.id}).id,
        })
        cls.colour = cls.env["product.attribute"].create({
            "name": "SKU Colour",
            "sku_part": "colour",
            "value_ids": [
                Command.create({"name": "Black", "sku_code": "002"}),
                Command.create({"name": "Navy", "sku_code": "005"}),
            ],
        })
        cls.size = cls.env["product.attribute"].create({
            "name": "SKU Size",
            "sku_part": "size",
            "value_ids": [
                Command.create({"name": "S", "sku_code": "02"}),
                Command.create({"name": "M", "sku_code": "03"}),
            ],
        })
        cls.material = cls.env["product.attribute"].create({
            "name": "Material",
            "value_ids": [Command.create({"name": "Nylon"})],
        })
        cls.user = new_test_user(cls.env, "sku_user", groups="base.group_user,product.group_product_manager")
        cls.sku_manager = new_test_user(cls.env, "sku_manager", groups="base.group_user,js_product_sku.group_sku_manager")

    def _create_product(self, **vals):
        return self.env["product.template"].create({
            "name": "Padded Dog Harness",
            "categ_id": self.harnesses.id,
            **vals,
        })

    def test_variant_sku(self):
        product = self._create_product(attribute_line_ids=[
            Command.create({"attribute_id": self.colour.id, "value_ids": [Command.set(self.colour.value_ids.ids)]}),
            Command.create({"attribute_id": self.size.id, "value_ids": [Command.set(self.size.value_ids.ids)]}),
        ])
        self.assertEqual(product.department_id, self.pets)
        self.assertEqual(product.territory_id, self.territory)
        self.assertEqual(product.unique_number, "0001")
        self.assertEqual(product.sku_base, "0130001")
        self.assertEqual(
            sorted(product.product_variant_ids.mapped("default_code")),
            ["013000100202", "013000100203", "013000100502", "013000100503"],
        )
        self.assertFalse(product.product_variant_ids.filtered("barcode"))

    def test_no_colour_no_size(self):
        product = self._create_product()
        self.assertEqual(product.default_code, "013000100000")

    def test_territory_version(self):
        product = self._create_product()
        self.assertEqual(product.default_code, "013000100000")
        # A version for another territory keeps the unique number.
        version = self._create_product(territory_id=self.territory_eu.id, unique_number="0001")
        self.assertEqual(version.default_code, "023000100000")
        self.assertEqual(self.pets.next_unique_number, 2)
        with self.assertRaises(ValidationError):
            product.territory_id = self.territory_eu
        with self.assertRaises(ValidationError):
            self.territory.code = "09"
        with self.assertRaises(ValidationError):
            self._create_product(territory_id=False)
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self.territory_eu.is_default = True

    def test_sequence_per_department(self):
        first, second = self.env["product.template"].create([
            {"name": "A", "categ_id": self.harnesses.id},
            {"name": "B", "categ_id": self.harnesses.id, "territory_id": self.territory_eu.id},
        ])
        other = self._create_product(categ_id=self.home.id)
        # One counter per department, shared by all territories.
        self.assertEqual((first.unique_number, second.unique_number), ("0001", "0002"))
        self.assertEqual(other.unique_number, "0001")
        self.assertEqual(self.pets.next_unique_number, 3)

    def test_form_assigns_number_on_save(self):
        self.pets.next_unique_number = 7
        form = Form(self.env["product.template"])
        form.name = "Dog Lead"
        form.categ_id = self.harnesses
        self.assertFalse(form.unique_number)
        # Another user saves a product in the same department meanwhile.
        self._create_product()
        self.assertEqual(form.save().default_code, "013000800000")
        self.assertEqual(self.pets.next_unique_number, 9)

    def test_manual_number_manager_only(self):
        product = self._create_product(unique_number="25")
        self.assertEqual(product.unique_number, "0025")
        self.assertEqual(self.pets.next_unique_number, 26)
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.cr.savepoint():
            self._create_product(unique_number="0025")
        product.with_user(self.sku_manager).unique_number = "30"
        self.assertEqual(product.default_code, "013003000000")
        product_user = product.with_user(self.user)
        with self.assertRaises(AccessError):
            product_user.unique_number = "0031"
        with self.assertRaises(AccessError):
            self.env["product.template"].with_user(self.user).create(
                {"name": "X", "categ_id": self.harnesses.id, "unique_number": "0031"})

    def test_user_gets_number(self):
        product = self.env["product.template"].with_user(self.user).create(
            {"name": "X", "categ_id": self.harnesses.id})
        self.assertEqual(product.default_code, "013000100000")

    def test_category_without_department_gets_number_later(self):
        misc = self.env["product.category"].create({"name": "Misc"})
        product = self._create_product(categ_id=misc.id)
        self.assertFalse(product.unique_number)
        product.with_user(self.user).categ_id = self.harnesses
        self.assertEqual(product.default_code, "013000100000")

    def test_sku_is_frozen(self):
        product = self._create_product(attribute_line_ids=[
            Command.create({"attribute_id": self.colour.id, "value_ids": [Command.set(self.colour.value_ids[0].ids)]}),
        ])
        with self.assertRaises(ValidationError):
            self.colour.value_ids[0].sku_code = "010"
        with self.assertRaises(ValidationError):
            product.categ_id = self.home
        with self.assertRaises(ValidationError):
            self.colour.sku_part = False
        with self.assertRaises(ValidationError):
            self.harnesses.parent_id = self.home
        with self.assertRaises(ValidationError):
            self.pets.department_code = "5"
        # Unused codes can still change.
        self.colour.value_ids[1].sku_code = "011"
        self.assertEqual(product.default_code, "013000100200")

    def test_other_variant_attribute_blocked(self):
        with self.assertRaises(ValidationError):
            self._create_product(attribute_line_ids=[
                Command.create({"attribute_id": self.material.id, "value_ids": [Command.set(self.material.value_ids.ids)]}),
            ])
        product = self._create_product()
        with self.assertRaises(ValidationError):
            product.attribute_line_ids = [
                Command.create({"attribute_id": self.material.id, "value_ids": [Command.set(self.material.value_ids.ids)]}),
            ]

    def test_duplicate_sku_blocked(self):
        self._create_product()
        with self.assertRaises(ValidationError):
            self.env["product.template"].create({"name": "Other", "default_code": "013000100000"})

    def test_products_without_sku_keep_reference(self):
        service = self.env["product.template"].create({"name": "Delivery", "type": "service", "default_code": "DEL"})
        self.assertEqual(service.default_code, "DEL")

    def test_validations(self):
        with self.assertRaises(ValidationError):
            self._create_product(unique_number="12a")
        with self.assertRaises(ValidationError):
            self.env["product.territory"].create({"code": "1", "name": "Bad"})
        with self.assertRaises(ValidationError):
            self.harnesses.department_code = "5"
        with self.assertRaises(ValidationError):
            self.size.value_ids[0].sku_code = "123"

    def test_department_code_added_later(self):
        garden = self.env["product.category"].create({"name": "Garden"})
        product = self._create_product(categ_id=garden.id)
        self.assertFalse(product.department_id)
        garden.department_code = "4"
        self.assertEqual(product.department_id, garden)

    def test_setup_wizard_keeps_old_numbers(self):
        garden = self.env["product.category"].create({"name": "Garden"})
        pots = self.env["product.category"].create({"name": "Pots", "parent_id": garden.id})
        navy, small = self.colour.value_ids[1], self.size.value_ids[0]
        old, new = self.env["product.template"].create([
            {"name": "Old Pot", "categ_id": pots.id, "attribute_line_ids": [
                Command.create({"attribute_id": self.colour.id, "value_ids": [Command.set(navy.ids)]}),
                Command.create({"attribute_id": self.size.id, "value_ids": [Command.set(small.ids)]}),
            ]},
            {"name": "New Pot", "categ_id": pots.id},
        ])
        old.product_variant_ids.default_code = "400700502"

        wizard = self.env["product.sku.setup"].with_user(self.sku_manager).create({})
        line = wizard.department_line_ids.filtered(lambda l: l.category_id == garden)
        self.assertEqual(line.department_code, "4", "read from the old reference")
        self.assertEqual(wizard.product_count, 2)
        wizard.action_run()

        self.assertEqual(garden.department_code, "4")
        self.assertEqual(old.product_variant_ids.default_code, "014000700502")
        self.assertEqual(new.default_code, "014000800000", "continues after the old numbers")
        self.assertEqual(garden.next_unique_number, 9)

    def test_setup_wizard_rejects_mismatched_reference(self):
        misc = self.env["product.category"].create({"name": "Misc"})
        product = self._create_product(categ_id=misc.id)
        product.default_code = "500700000"
        wizard = self.env["product.sku.setup"].create({})
        # The old reference says department 5, but Misc becomes department 6
        wizard.department_line_ids.filtered(lambda l: l.category_id == misc).department_code = "6"
        with self.assertRaises(UserError):
            wizard.action_run()
