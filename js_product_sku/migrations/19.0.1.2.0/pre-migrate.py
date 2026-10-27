def migrate(cr, version):
    # Territory 01 may have been created by hand before it came with the module:
    # use that record instead of creating a second one with the same code.
    cr.execute("""
        INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
        SELECT 'js_product_sku', 'territory_uk', 'product.territory', id, true
          FROM product_territory WHERE code = '01'
        ON CONFLICT DO NOTHING
    """)
