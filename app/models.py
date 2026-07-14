from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy

# To avoid circular imports, we can define the db object here and import it in the app factory
db = SQLAlchemy()

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='staff') # 'admin' or 'staff'
    phone = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active') # 'active' or 'disabled'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def is_active(self):
        return self.status == 'active'

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class FuelType(db.Model):
    __tablename__ = 'fuel_types'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False) # e.g. 'Petrol', 'Diesel'
    unit = db.Column(db.String(20), nullable=False, default='Liter')
    
    # Relationships
    prices = db.relationship('FuelPrice', backref='fuel_type', lazy=True, cascade='all, delete-orphan')
    stock_entries = db.relationship('StockEntry', backref='fuel_type', lazy=True, cascade='all, delete-orphan')
    inventory = db.relationship('Inventory', backref='fuel_type', uselist=False, lazy=True, cascade='all, delete-orphan')
    meter_readings = db.relationship('MeterReading', backref='fuel_type', lazy=True, cascade='all, delete-orphan')
    sales = db.relationship('Sale', backref='fuel_type', lazy=True)

    def __repr__(self):
        return f"<FuelType {self.name}>"


class FuelPrice(db.Model):
    __tablename__ = 'fuel_prices'
    
    id = db.Column(db.Integer, primary_key=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    price_per_liter = db.Column(db.Numeric(10, 2), nullable=False)
    effective_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    updater = db.relationship('User', foreign_keys=[updated_by])

    def __repr__(self):
        return f"<FuelPrice {self.fuel_type_id}: {self.price_per_liter} on {self.effective_date}>"


class Inventory(db.Model):
    __tablename__ = 'inventory'
    
    id = db.Column(db.Integer, primary_key=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), unique=True, nullable=False)
    current_stock_liters = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reorder_threshold = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)

    def __repr__(self):
        return f"<Inventory {self.fuel_type_id}: {self.current_stock_liters} liters>"


class StockEntry(db.Model):
    __tablename__ = 'stock_entries'
    
    id = db.Column(db.Integer, primary_key=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    liters_added = db.Column(db.Numeric(12, 2), nullable=False)
    cost_per_liter = db.Column(db.Numeric(10, 2), nullable=False)
    supplier = db.Column(db.String(100), nullable=True)
    entry_date = db.Column(db.DateTime, default=datetime.utcnow)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    creator = db.relationship('User', foreign_keys=[added_by])

    def __repr__(self):
        return f"<StockEntry {self.fuel_type_id}: +{self.liters_added} liters from {self.supplier}>"


class Machine(db.Model):
    __tablename__ = 'machines'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    fuel_type = db.relationship('FuelType', foreign_keys=[fuel_type_id])

    def __repr__(self):
        return f"<Machine {self.name}>"


class MeterReading(db.Model):
    __tablename__ = 'meter_readings'
    
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey('machines.id'), nullable=True)
    dispenser_nozzle_id = db.Column(db.String(50), nullable=True)  # legacy
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    opening_reading = db.Column(db.Numeric(12, 2), nullable=False)
    closing_reading = db.Column(db.Numeric(12, 2), nullable=True)
    liters_sold = db.Column(db.Numeric(12, 2), nullable=True, default=0.00)
    reading_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    closed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    
    machine = db.relationship('Machine', foreign_keys=[machine_id], backref='meter_readings')
    recorder = db.relationship('User', foreign_keys=[recorded_by])
    closer = db.relationship('User', foreign_keys=[closed_by])

    @property
    def is_closed(self):
        return self.closing_reading is not None

    def __repr__(self):
        return f"<MeterReading machine={self.machine_id} {self.opening_reading}->{self.closing_reading}>"


class CreditSale(db.Model):
    """Item/credit sale, advance, or loan. Fuel ledger-only; shop items decrease stock."""
    __tablename__ = 'credit_sales'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)  # null = walk-in paid
    machine_id = db.Column(db.Integer, db.ForeignKey('machines.id'), nullable=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=True)
    other_item_id = db.Column(db.Integer, db.ForeignKey('other_items.id'), nullable=True)
    sale_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    vehicle_number = db.Column(db.String(50), nullable=True)
    liters = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)  # liters/qty; 0 for advance/loan
    rate = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)  # cash received now
    entry_type = db.Column(db.String(20), nullable=False, default='sale')  # sale | advance | loan
    payment_status = db.Column(db.String(20), nullable=False, default='unpaid')  # paid / unpaid / partial
    remarks = db.Column(db.String(255), nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', foreign_keys=[customer_id], backref='credit_sales')
    machine = db.relationship('Machine', foreign_keys=[machine_id])
    fuel_type = db.relationship('FuelType', foreign_keys=[fuel_type_id])
    other_item = db.relationship('OtherItem', foreign_keys=[other_item_id])
    recorder = db.relationship('User', foreign_keys=[recorded_by])

    @property
    def item_name(self):
        if self.entry_type == 'advance':
            return 'Customer Advance'
        if self.entry_type == 'loan':
            return 'Customer Loan / Borrow'
        if self.other_item:
            return self.other_item.display_name()
        if self.fuel_type:
            return self.fuel_type.name
        return 'Item'

    @property
    def is_fuel(self):
        return self.fuel_type_id is not None

    @property
    def credit_amount(self):
        """Portion still owed on this entry."""
        return max(float(self.amount or 0) - float(self.amount_paid or 0), 0.0)

    def __repr__(self):
        return f"<CreditSale {self.id} type={self.entry_type} amount={self.amount}>"


class Expense(db.Model):
    __tablename__ = 'expenses'

    id = db.Column(db.Integer, primary_key=True)
    expense_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    recorder = db.relationship('User', foreign_keys=[recorded_by])

    def __repr__(self):
        return f"<Expense {self.name}: {self.amount}>"


class DailyCashCount(db.Model):
    """Physical cash counted at till for a day (for journal reconciliation)."""
    __tablename__ = 'daily_cash_counts'

    id = db.Column(db.Integer, primary_key=True)
    count_date = db.Column(db.Date, nullable=False, unique=True)
    cash_in_hand = db.Column(db.Numeric(12, 2), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    recorder = db.relationship('User', foreign_keys=[recorded_by])

    def __repr__(self):
        return f"<DailyCashCount {self.count_date}: {self.cash_in_hand}>"


class DailyFuelStock(db.Model):
    __tablename__ = 'daily_fuel_stocks'

    id = db.Column(db.Integer, primary_key=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    stock_date = db.Column(db.Date, nullable=False, default=datetime.utcnow().date)
    opening_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    received_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    sold_stock = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    closing_stock = db.Column(db.Numeric(12, 2), nullable=True)
    is_closed = db.Column(db.Boolean, nullable=False, default=False)
    opened_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    closed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    fuel_type = db.relationship('FuelType', foreign_keys=[fuel_type_id])

    def __repr__(self):
        return f"<DailyFuelStock {self.fuel_type_id} {self.stock_date}>"


class Customer(db.Model):
    __tablename__ = 'customers'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(200), nullable=True)
    credit_limit = db.Column(db.Numeric(12, 2), nullable=True)
    current_balance_due = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    sales = db.relationship('Sale', backref='customer', lazy=True)
    payments = db.relationship('Payment', backref='customer', lazy=True)

    def __repr__(self):
        return f"<Customer {self.name} (Due: {self.current_balance_due})>"


class Sale(db.Model):
    __tablename__ = 'sales'
    
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True) # Null = walk-in cash
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=False)
    liters = db.Column(db.Numeric(12, 2), nullable=False)
    price_per_liter = db.Column(db.Numeric(10, 2), nullable=False) # Snapshot at sale time
    total_amount = db.Column(db.Numeric(12, 2), nullable=False) # Computed liters * price_per_liter
    payment_type = db.Column(db.String(20), nullable=False, default='cash') # 'cash' or 'credit'
    sale_date = db.Column(db.DateTime, default=datetime.utcnow)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    recorder = db.relationship('User', foreign_keys=[recorded_by])

    def __repr__(self):
        return f"<Sale {self.id}: {self.liters}L of fuel_type_id {self.fuel_type_id}>"


class Payment(db.Model):
    __tablename__ = 'payments'
    
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    method = db.Column(db.String(50), nullable=False, default='Cash') # 'Cash', 'Bank Transfer', etc.
    note = db.Column(db.String(200), nullable=True)

    def __repr__(self):
        return f"<Payment {self.id}: {self.amount_paid} from customer {self.customer_id}>"


class OtherItem(db.Model):
    __tablename__ = 'other_items'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(20), nullable=False, default='other')  # mobile, filter, other
    name = db.Column(db.String(100), nullable=False)
    company = db.Column(db.String(100), nullable=True)
    item_type = db.Column(db.String(100), nullable=True)
    vendor = db.Column(db.String(100), nullable=True)
    cost_price = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    sale_price = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    liters = db.Column(db.Numeric(12, 2), nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def display_name(self):
        parts = [self.name]
        if self.company:
            parts.append(f"({self.company})")
        if self.item_type:
            parts.append(f"— {self.item_type}")
        return " ".join(parts)

    def __repr__(self):
        return f"<OtherItem {self.category}:{self.name} qty={self.quantity}>"


class ItemPurchaseLog(db.Model):
    __tablename__ = 'item_purchase_logs'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(20), nullable=False)  # fuel, mobile, filter, other
    item_name = db.Column(db.String(100), nullable=False)
    company = db.Column(db.String(100), nullable=True)
    item_type = db.Column(db.String(100), nullable=True)
    vendor = db.Column(db.String(100), nullable=True)
    cost_price = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    sale_price = db.Column(db.Numeric(10, 2), nullable=False, default=0.00)
    quantity = db.Column(db.Integer, nullable=True)
    liters = db.Column(db.Numeric(12, 2), nullable=True)
    fuel_type_id = db.Column(db.Integer, db.ForeignKey('fuel_types.id'), nullable=True)
    entry_date = db.Column(db.DateTime, default=datetime.utcnow)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    fuel_type = db.relationship('FuelType', foreign_keys=[fuel_type_id])
    creator = db.relationship('User', foreign_keys=[added_by])

    def __repr__(self):
        return f"<ItemPurchaseLog {self.category}:{self.item_name}>"


class ItemPriceLog(db.Model):
    __tablename__ = 'item_price_logs'

    id = db.Column(db.Integer, primary_key=True)
    other_item_id = db.Column(db.Integer, db.ForeignKey('other_items.id'), nullable=False)
    sale_price = db.Column(db.Numeric(10, 2), nullable=False)
    cost_price = db.Column(db.Numeric(10, 2), nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship('OtherItem', foreign_keys=[other_item_id])
    updater = db.relationship('User', foreign_keys=[updated_by])

    def __repr__(self):
        return f"<ItemPriceLog item={self.other_item_id} price={self.sale_price}>"
