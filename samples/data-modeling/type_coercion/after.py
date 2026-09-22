def total_cents(amounts_in_dollars):
    """amounts_in_dollars: list[str] like ['12.50', '3.00']."""
    return sum(int(float(a) * 100) for a in amounts_in_dollars)
