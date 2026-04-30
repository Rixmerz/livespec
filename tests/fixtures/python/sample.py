"""Python fixture: 2 top-level fns + 1 class with 2 methods + 1 cross-call."""


def top_level_one(x):
    return helper(x)


def top_level_two(x):
    return x + 1


def helper(x):
    return x * 2


class Greeter:
    def __init__(self, name):
        self.name = name

    def greet(self):
        return helper(len(self.name))
