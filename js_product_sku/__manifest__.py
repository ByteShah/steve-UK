{
    "name": "Product SKU",
    "summary": "12-digit SKU per variant: territory, department, unique number, colour and size (TT D UUUU CCC SS).",
    "description": """
Every variant gets a 12-digit numeric SKU in its Internal Reference::

    TT  D  UUUU  CCC  SS
    01  3  0005  005  03   ->  013000500503

* TT   code of the product's territory (Inventory > Configuration > Territories), the default territory proposed
* D    department code of the product category's top-level category
* UUUU unique number per department across all companies and territories, assigned on save (SKU managers can change it)
* CCC  SKU code of the value of the attribute whose SKU Part is Colour (000 if none)
* SS   SKU code of the value of the attribute whose SKU Part is Size (00 if none)

Inventory > Configuration > SKU Setup sets the codes and gives existing products their SKU,
keeping the numbers of old 9-digit references (D UUU CCC SS).
""",
    "version": "19.0.1.2.0",
    "category": "Inventory/Inventory",
    "author": "Jaimin Shah",
    "license": "LGPL-3",
    "depends": ["product", "stock"],
    "data": [
        "security/product_sku_security.xml",
        "security/ir.model.access.csv",
        "data/product_territory_data.xml",
        "views/product_territory_views.xml",
        "views/product_category_views.xml",
        "views/product_attribute_views.xml",
        "views/product_template_views.xml",
        "wizard/product_sku_setup_views.xml",
    ],
    "installable": True,
}
