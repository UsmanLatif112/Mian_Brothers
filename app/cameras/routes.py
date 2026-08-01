from urllib.parse import urlparse

import requests
from flask import (
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required

from app.cameras import cameras_bp
from app.decorators import role_required
from app.models import Camera, db

ALLOWED_STREAM_TYPES = ('mjpeg', 'snapshot', 'hls', 'iframe')
PROXY_TIMEOUT = (5, 60)


def _safe_http_url(url):
    """Only allow http/https remote camera endpoints for proxying."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def _camera_auth(camera):
    if camera.username:
        return (camera.username, camera.password or '')
    return None


@cameras_bp.route('/')
@login_required
def index():
    cameras = (
        Camera.query
        .filter_by(is_active=True)
        .order_by(Camera.sort_order.asc(), Camera.name.asc())
        .all()
    )
    return render_template('cameras/index.html', cameras=cameras)


@cameras_bp.route('/manage', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def manage():
    if request.method == 'POST':
        action = request.form.get('action', 'create')

        if action == 'create':
            name = (request.form.get('name') or '').strip()
            location = (request.form.get('location') or '').strip() or None
            stream_type = (request.form.get('stream_type') or 'mjpeg').strip().lower()
            stream_url = (request.form.get('stream_url') or '').strip()
            username = (request.form.get('username') or '').strip() or None
            password = request.form.get('password') or None
            use_proxy = request.form.get('use_proxy') == '1'
            refresh_raw = request.form.get('refresh_seconds') or '2'
            sort_raw = request.form.get('sort_order') or '0'

            if not name:
                flash('Camera name is required.', 'danger')
                return redirect(url_for('cameras.manage'))
            if not stream_url:
                flash('Stream URL is required.', 'danger')
                return redirect(url_for('cameras.manage'))
            if stream_type not in ALLOWED_STREAM_TYPES:
                flash('Invalid stream type.', 'danger')
                return redirect(url_for('cameras.manage'))
            if not _safe_http_url(stream_url):
                flash('Stream URL must start with http:// or https://', 'danger')
                return redirect(url_for('cameras.manage'))

            try:
                refresh_seconds = max(1, int(refresh_raw))
                sort_order = int(sort_raw)
            except (TypeError, ValueError):
                flash('Invalid refresh interval or sort order.', 'danger')
                return redirect(url_for('cameras.manage'))

            # HLS/iframe cannot be usefully proxied as simple image streams
            if stream_type in ('hls', 'iframe'):
                use_proxy = False

            camera = Camera(
                name=name,
                location=location,
                stream_type=stream_type,
                stream_url=stream_url,
                username=username,
                password=password if username else None,
                use_proxy=use_proxy,
                refresh_seconds=refresh_seconds,
                sort_order=sort_order,
                is_active=True,
                created_by=current_user.id,
            )
            db.session.add(camera)
            db.session.commit()
            flash(f'Camera "{name}" added.', 'success')
            return redirect(url_for('cameras.manage'))

        if action == 'update':
            camera = Camera.query.get(request.form.get('camera_id'))
            if not camera:
                flash('Camera not found.', 'danger')
                return redirect(url_for('cameras.manage'))

            name = (request.form.get('name') or '').strip()
            location = (request.form.get('location') or '').strip() or None
            stream_type = (request.form.get('stream_type') or camera.stream_type).strip().lower()
            stream_url = (request.form.get('stream_url') or '').strip()
            username = (request.form.get('username') or '').strip() or None
            password = request.form.get('password')
            use_proxy = request.form.get('use_proxy') == '1'
            is_active = request.form.get('is_active') == '1'

            if not name or not stream_url:
                flash('Name and stream URL are required.', 'danger')
                return redirect(url_for('cameras.manage'))
            if stream_type not in ALLOWED_STREAM_TYPES:
                flash('Invalid stream type.', 'danger')
                return redirect(url_for('cameras.manage'))
            if not _safe_http_url(stream_url):
                flash('Stream URL must start with http:// or https://', 'danger')
                return redirect(url_for('cameras.manage'))

            try:
                refresh_seconds = max(1, int(request.form.get('refresh_seconds') or camera.refresh_seconds))
                sort_order = int(request.form.get('sort_order') or camera.sort_order)
            except (TypeError, ValueError):
                flash('Invalid refresh interval or sort order.', 'danger')
                return redirect(url_for('cameras.manage'))

            if stream_type in ('hls', 'iframe'):
                use_proxy = False

            camera.name = name
            camera.location = location
            camera.stream_type = stream_type
            camera.stream_url = stream_url
            camera.username = username
            # Keep existing password when the field is left blank
            if password:
                camera.password = password
            elif not username:
                camera.password = None
            camera.use_proxy = use_proxy
            camera.refresh_seconds = refresh_seconds
            camera.sort_order = sort_order
            camera.is_active = is_active
            db.session.commit()
            flash(f'Camera "{name}" updated.', 'success')
            return redirect(url_for('cameras.manage'))

        if action == 'delete':
            camera = Camera.query.get(request.form.get('camera_id'))
            if camera:
                label = camera.name
                db.session.delete(camera)
                db.session.commit()
                flash(f'Camera "{label}" deleted.', 'success')
            return redirect(url_for('cameras.manage'))

        flash('Unknown action.', 'danger')
        return redirect(url_for('cameras.manage'))

    cameras = Camera.query.order_by(Camera.sort_order.asc(), Camera.name.asc()).all()
    return render_template(
        'cameras/manage.html',
        cameras=cameras,
        stream_types=ALLOWED_STREAM_TYPES,
    )


@cameras_bp.route('/<int:camera_id>/stream')
@login_required
def stream(camera_id):
    """Proxy MJPEG/snapshot from the camera so credentials stay server-side."""
    camera = Camera.query.get_or_404(camera_id)
    if not camera.is_active:
        abort(404)
    if camera.stream_type not in ('mjpeg', 'snapshot'):
        abort(400)
    if not _safe_http_url(camera.stream_url):
        abort(400)

    auth = _camera_auth(camera)
    headers = {'User-Agent': 'OctaneFlow-CameraProxy/1.0'}

    try:
        upstream = requests.get(
            camera.stream_url,
            auth=auth,
            headers=headers,
            stream=True,
            timeout=PROXY_TIMEOUT,
        )
    except requests.RequestException:
        abort(502)

    if upstream.status_code >= 400:
        upstream.close()
        abort(upstream.status_code if upstream.status_code < 500 else 502)

    content_type = upstream.headers.get('Content-Type', 'multipart/x-mixed-replace')

    @stream_with_context
    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        generate(),
        status=200,
        headers={
            'Content-Type': content_type,
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
