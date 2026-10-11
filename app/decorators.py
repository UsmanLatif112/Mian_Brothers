from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user


def role_required(*roles):
    """
    Restrict access to one or more exact roles.

    Examples:
      @role_required('super_admin')
      @role_required('super_admin', 'admin')

    Legacy aliases: 'staff' counts as 'user'.
    """
    if len(roles) == 1 and isinstance(roles[0], (list, tuple, set)):
        roles = tuple(roles[0])

    allowed = set()
    for role in roles:
        if role in ('user', 'staff'):
            allowed.update({'user', 'staff'})
        else:
            allowed.add(role)

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))

            user_role = current_user.role or ''
            if user_role not in allowed:
                if allowed == {'super_admin'}:
                    label = 'super admin'
                elif allowed <= {'super_admin', 'admin'}:
                    label = 'admin'
                else:
                    label = 'authorized'
                flash(
                    f"Unauthorized access. This area is restricted to {label} users.",
                    "danger",
                )
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator
