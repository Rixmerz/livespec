def handle_pay(order):
    return _charge(order) * 2


def handle_refund(order):
    return -order


def handle_legacy_export(order):
    return str(order)


def _charge(order):
    return order + 1
