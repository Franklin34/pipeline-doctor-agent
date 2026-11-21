from app.pricing import apply_discount


def test_apply_discount():
    assert apply_discount(100.0, 0.1) == 90.0


def test_apply_discount_quarter():
    assert apply_discount(80.0, 0.25) == 60.0
