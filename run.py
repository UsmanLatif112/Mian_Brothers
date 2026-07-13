import os
from app import create_app

app = create_app()

if __name__ == '__main__':
    # Run the server on port 5000 in debug mode
    app.run(
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', 5000)),
        debug=True
    )
