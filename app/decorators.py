from functools import wraps
from flask import abort, flash, redirect, url_for
from flask_login import current_user

def role_required(role):
    """
    Decorator to restrict access to routes based on user roles.
    Allowed roles: 'admin', 'staff'.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if current_user.role != role:
                flash(f"Unauthorized access. This area is restricted to {role} users.", "danger")
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator
