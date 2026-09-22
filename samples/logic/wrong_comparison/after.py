def is_eligible(age, income):
    """Eligible if age is at least 18 and income is below the cap."""
    return age >= 18 and income <= 50000
