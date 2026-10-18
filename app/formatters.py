"""Display number formatting — strip trailing zeros (.0 / .00 / .000)."""


def format_number(value, max_decimals=3, default='0'):
    """
    Format a number with thousands separators; drop useless trailing zeros.
    4 → '4', 4.5 → '4.5', 95.25 → '95.25', 450.00 → '450'
    """
    if value is None:
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    rounded = round(x, max_decimals)
    # Avoid "-0"
    if rounded == 0:
        rounded = 0.0
    text = f'{rounded:,.{max_decimals}f}'
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text


def format_money(value, max_decimals=2, prefix='PKR ', default=None):
    """PKR amount without trailing zeros (450 not 450.00)."""
    if default is None:
        default = f'{prefix}0'
    if value is None:
        return default
    try:
        float(value)
    except (TypeError, ValueError):
        return default
    return f'{prefix}{format_number(value, max_decimals=max_decimals, default="0")}'
