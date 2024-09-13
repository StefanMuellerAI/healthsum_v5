from flask_socketio import SocketIO

socketio = SocketIO(message_queue='redis://localhost:6380/0', async_mode='eventlet')  # Initialisierung ohne App