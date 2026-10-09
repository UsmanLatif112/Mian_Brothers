import os
import uuid
from flask import (
    render_template, redirect, url_for, flash, request, current_app,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from app.auth import auth_bp
from app.models import db, User, Agency
from app.decorators import role_required

ALLOWED_LOGO_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}


def _save_agency_logo(file_storage):
    """Save uploaded logo into uploads/; return filename or None."""
    if not file_storage or not getattr(file_storage, 'filename', None):
        return None
    raw = secure_filename(file_storage.filename)
    ext = os.path.splitext(raw)[1].lower()
    if ext not in ALLOWED_LOGO_EXT:
        return None
    filename = f"agency_{uuid.uuid4().hex[:12]}{ext}"
    upload_dir = current_app.config.get('UPLOAD_FOLDER')
    os.makedirs(upload_dir, exist_ok=True)
    file_storage.save(os.path.join(upload_dir, filename))
    return filename


def _seed_agency_defaults(agency_id):
    """Fuel types + shop categories for a newly created agency."""
    try:
        from app import ensure_default_fuel_types
        ensure_default_fuel_types(agency_id=agency_id)
    except Exception as e:
        print(f'fuel seed for agency {agency_id}: {e}')
    try:
        from app.inventory.categories import ensure_shop_categories
        ensure_shop_categories(agency_id=agency_id)
    except Exception as e:
        print(f'category seed for agency {agency_id}: {e}')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
        
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False

        user = User.query.filter_by(name=username).first()

        if not user or not user.check_password(password):
            flash('Please check your login credentials and try again.', 'danger')
            return redirect(url_for('auth.login'))

        if user.status != 'active':
            flash('Your account has been disabled. Please contact the administrator.', 'danger')
            return redirect(url_for('auth.login'))

        login_user(user, remember=remember)
        next_page = request.args.get('next')
        return redirect(next_page) if next_page else redirect(url_for('dashboard.index'))

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/users', methods=['GET', 'POST'])
@auth_bp.route('/staff-accounts', methods=['GET', 'POST'])  # legacy alias
@login_required
@role_required('super_admin')
def users():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'create':
            name = (request.form.get('name') or '').strip()
            email = (request.form.get('email') or '').strip()
            phone = (request.form.get('phone') or '').strip() or None
            password = request.form.get('password')
            agency_name = (request.form.get('agency_name') or '').strip()
            agency_address = (request.form.get('agency_address') or '').strip() or None
            agency_phone = (request.form.get('agency_phone') or '').strip() or None
            agency_email = (request.form.get('agency_email') or '').strip() or None
            
            if not name or not email or not password:
                flash('Username, email, and password are required.', 'danger')
                return redirect(url_for('auth.users'))
            if not agency_name:
                flash('Agency / company name is required.', 'danger')
                return redirect(url_for('auth.users'))

            if User.query.filter_by(name=name).first():
                flash('A user with that username already exists.', 'danger')
                return redirect(url_for('auth.users'))
            if User.query.filter_by(email=email).first():
                flash('A user with that email already exists.', 'danger')
                return redirect(url_for('auth.users'))

            logo_name = _save_agency_logo(request.files.get('agency_logo'))
            agency = Agency(
                name=agency_name,
                address=agency_address,
                phone=agency_phone,
                email=agency_email,
                logo=logo_name,
                is_active=True,
            )
            db.session.add(agency)
            db.session.flush()

            new_user = User(
                name=name,
                email=email,
                phone=phone,
                role='user',
                status='active',
                agency_id=agency.id,
            )
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()

            _seed_agency_defaults(agency.id)
            
            flash(f"User '{name}' created for agency '{agency_name}'.", 'success')
            
        elif action == 'edit':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user:
                user.name = (request.form.get('name') or '').strip() or user.name
                user.phone = (request.form.get('phone') or '').strip() or None
                
                new_password = request.form.get('password')
                if new_password:
                    user.set_password(new_password)

                # Update linked agency contact / logo
                agency = Agency.query.get(user.agency_id) if user.agency_id else None
                if agency:
                    agency_name = (request.form.get('agency_name') or '').strip()
                    if agency_name:
                        agency.name = agency_name
                    agency.address = (request.form.get('agency_address') or '').strip() or None
                    agency.phone = (request.form.get('agency_phone') or '').strip() or None
                    agency.email = (request.form.get('agency_email') or '').strip() or None
                    new_logo = _save_agency_logo(request.files.get('agency_logo'))
                    if new_logo:
                        agency.logo = new_logo
                    
                db.session.commit()
                flash(f"User '{user.name}' updated successfully.", 'success')
                
        elif action == 'toggle_status':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user:
                if user.id == current_user.id:
                    flash('You cannot disable your own account!', 'danger')
                else:
                    user.status = 'disabled' if user.status == 'active' else 'active'
                    db.session.commit()
                    flash(f"User status updated to '{user.status}' for {user.name}.", 'success')
                    
        return redirect(url_for('auth.users'))
        
    # GET request
    search_q = (request.args.get('search') or '').strip().lower()
    users = User.query.order_by(User.id.asc()).all()
    if search_q:
        users = [
            u for u in users
            if search_q in (u.name or '').lower()
            or search_q in (u.email or '').lower()
            or search_q in (u.phone or '').lower()
            or search_q in (u.role or '').lower()
            or (u.agency and search_q in (u.agency.name or '').lower())
        ]
    return render_template('auth/users.html', users=users, search=search_q)


# Keep old endpoint name for any lingering links
staff_accounts = users
