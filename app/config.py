import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-12345')

    # MySQL (preferred when DB_* vars are set)
    DB_USER = os.environ.get('DB_USER')
    DB_PASSWORD = os.environ.get('DB_PASSWORD')
    DB_HOST = os.environ.get('DB_HOST', 'localhost')
    DB_PORT = os.environ.get('DB_PORT', '3306')
    DB_NAME = os.environ.get('DB_NAME')

    if DB_USER and DB_PASSWORD and DB_NAME:
        _password = quote_plus(DB_PASSWORD)
        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{DB_USER}:{_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
            f"?charset=utf8mb4"
        )
    else:
        # Fallback: SQLite in project instance/ folder
        _base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        _instance_dir = os.path.join(_base_dir, 'instance')
        os.makedirs(_instance_dir, exist_ok=True)
        _db_path = os.path.join(_instance_dir, 'petrol_pump.db').replace('\\', '/')
        SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', f'sqlite:///{_db_path}')

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
    }

    # Full-data backups (JSON dumps under project /backups)
    _base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(_base_dir, 'backups'))
