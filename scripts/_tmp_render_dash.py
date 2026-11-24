from app import create_app
from app.models import User

app = create_app()
with app.app_context():
    user = User.query.filter_by(status='active').order_by(User.id.asc()).first()
    uid = user.id if user else None
print('USER', uid)
with app.test_client() as client:
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    page = client.get('/dashboard/?period=all')
    html = page.get_data(as_text=True)
    print('STATUS', page.status_code)
    start = html.find('kpi-credit')
    print(html[start:start + 700] if start >= 0 else 'MISSING')
