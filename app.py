import hivemind
from flask import Flask, make_response
from flask_cors import CORS
from flask_sock import Sock

import utils
import views

logger = hivemind.get_logger(__file__)

logger.info("Loading models")
models = utils.load_models()

logger.info("Starting Flask app")
app = Flask(__name__)
CORS(app)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)

logger.info("Pre-rendering index page")
index_html = views.render_index(app)


@app.route("/")
def main_page():
    response = make_response(index_html)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


import status_api
import http_api
import websocket_api
