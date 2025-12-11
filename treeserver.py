from flask import Flask, request
from flask_sockets import Sockets
import threading
import queue

app = Flask(__name__)
sockets = Sockets(app)

# Coda per scambio messaggi tra PC e frontend
msg_queue = queue.Queue()
pc_socket = None  # Connessione PC

@sockets.route('/pc')
def pc_ws(ws):
    global pc_socket
    pc_socket = ws
    while not ws.closed:
        # Riceve messaggi dal PC (risposte)
        msg = ws.receive()
        if msg:
            msg_queue.put(msg)

@app.route('/ai', methods=['POST'])
def ai():
    global pc_socket
    if not pc_socket:
        return {"error": "PC non connesso"}, 503
    question = request.json["question"]
    # Invia domanda al PC
    pc_socket.send(question)
    # Attendi risposta
    answer = msg_queue.get()
    return {"response": answer}

if __name__ == "__main__":
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(('', 5000), app, handler_class=WebSocketHandler)
    server.serve_forever()
