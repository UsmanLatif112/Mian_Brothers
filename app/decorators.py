from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user


def role_required(role):
    """
    Restrict access by role.

    Allowed roles: 'super_admin', 'user' (legacy aliases: 'admin', 'staff').
    'admin' and 'super_admin' are treated as equivalent for access checks.
    'staff' and 'user' are treated as equivalent.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))

            user_role = current_user.role or ''
            allowed = {role}
            if role in ('super_admin', 'admin'):
                allowed = {'super_admin', 'admin'}
            elif role in ('user', 'staff'):
                allowed = {'user', 'staff'}

            if user_role not in allowed:
                label = 'super admin' if role in ('super_admin', 'admin') else role
                flash(
                    f"Unauthorized access. This area is restricted to {label} users.",
                    "danger",
                )
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator
