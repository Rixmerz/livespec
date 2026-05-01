def list_tools():
    return []


def _cosine(a, b):
    return 0.0


def search(query):
    rows = list_tools()
    score = _cosine(query, rows)
    return score
