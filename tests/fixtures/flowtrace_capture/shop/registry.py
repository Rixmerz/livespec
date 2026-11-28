from shop import handlers

REGISTRY = {"pay": "handle_pay", "refund": "handle_refund"}


def dispatch(kind, order):
    return getattr(handlers, REGISTRY[kind])(order)
