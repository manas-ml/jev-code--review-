def last_n_items(items, n):
    """Return the last n items of a list."""
    if n <= 0:
        return []
    return items[len(items) - n - 1:]
