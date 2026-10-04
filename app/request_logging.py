"""Request/response logging for cPanel terminal (tail -f logs/app.log)."""
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from flask import g, request


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def setup_request_logging(app):
    """Attach file + stderr handlers and log each request/response."""
    log_dir = os.path.join(_project_root(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'app.log')

    level_name = (os.environ.get('LOG_LEVEL') or 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    root_name = 'octaneflow'
    logger = logging.getLogger(root_name)
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers on app reload / create_app() twice
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8',
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    # Also send Flask's app.logger to the same file
    app.logger.handlers = []
    app.logger.setLevel(level)
    for h in logger.handlers:
        app.logger.addHandler(h)
    app.logger.propagate = False

    skip_prefixes = (
        '/static/',
        '/favicon.ico',
    )

    @app.before_request
    def _log_request_start():
        g._req_started = time.perf_counter()
        path = request.path or ''
        if any(path.startswith(p) for p in skip_prefixes):
            g._req_skip_log = True
            return
        g._req_skip_log = False

        user = '-'
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                user = f"{current_user.id}:{getattr(current_user, 'name', '') or current_user.get_id()}"
        except Exception:
            pass

        qs = request.query_string.decode('utf-8', errors='replace') if request.query_string else ''
        logger.info(
            'LOAD  %s %s%s | ip=%s | user=%s | endpoint=%s',
            request.method,
            path,
            f'?{qs}' if qs else '',
            request.headers.get('X-Forwarded-For', request.remote_addr) or '-',
            user,
            request.endpoint or '-',
        )

    @app.after_request
    def _log_request_end(response):
        if getattr(g, '_req_skip_log', False):
            return response

        started = getattr(g, '_req_started', None)
        ms = (time.perf_counter() - started) * 1000.0 if started is not None else -1
        path = request.path or ''
        logger.info(
            'RESP  %s %s -> %s | %s | %.0fms | type=%s',
            request.method,
            path,
            response.status_code,
            response.status,
            ms,
            (response.mimetype or '-')[:48],
        )
        return response

    @app.teardown_request
    def _log_request_error(exc):
        if exc is None or getattr(g, '_req_skip_log', False):
            return
        logger.exception(
            'ERROR %s %s | %s',
            request.method,
            request.path,
            exc,
        )

    logger.info('Logging ready -> %s (also stderr). Tail with: tail -f logs/app.log', log_path)
    return logger
