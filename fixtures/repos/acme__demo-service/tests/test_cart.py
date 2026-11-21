from app.cart import total


def test_total():
    assert total([1.0, 2.5]) == 3.5
