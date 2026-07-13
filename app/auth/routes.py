from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.auth import auth_bp
from app.models import db, User
from app.decorators import role_required

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False
        
        user = User.query.filter_by(email=email).first()
        
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

@auth_bp.route('/staff-accounts', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def staff_accounts():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'create':
            name = request.form.get('name')
            email = request.form.get('email')
            phone = request.form.get('phone')
            password = request.form.get('password')
            role = request.form.get('role', 'staff')
            
            # Validation
            if not name or not email or not password:
                flash('Name, email, and password are required fields.', 'danger')
                return redirect(url_for('auth.staff_accounts'))
                
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('A user with that email already exists.', 'danger')
                return redirect(url_for('auth.staff_accounts'))
                
            new_user = User(name=name, email=email, phone=phone, role=role, status='active')
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            
            flash(f"User '{name}' has been created successfully as {role}.", 'success')
            
        elif action == 'edit':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user:
                user.name = request.form.get('name')
                user.phone = request.form.get('phone')
                user.role = request.form.get('role', 'staff')
                
                new_password = request.form.get('password')
                if new_password: # Update password only if provided
                    user.set_password(new_password)
                    
                db.session.commit()
                flash(f"User '{user.name}' updated successfully.", 'success')
                
        elif action == 'toggle_status':
            user_id = request.form.get('user_id')
            user = User.query.get(user_id)
            if user:
                if user.id == current_user.id:
                    flash('You cannot disable your own admin account!', 'danger')
                else:
                    user.status = 'disabled' if user.status == 'active' else 'active'
                    db.session.commit()
                    flash(f"User status updated to '{user.status}' for {user.name}.", 'success')
                    
        return redirect(url_for('auth.staff_accounts'))
        
    # GET request
    users = User.query.all()
    return render_template('auth/staff.html', users=users)
