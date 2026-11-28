from shop.registry import dispatch

print(dispatch("pay", 10), dispatch("refund", 3))
