import hivemind
from flask import Flask, make_response
from flask_cors import CORS

from pollen.features.chat import utils
import views
from pollen.core.extensions import sock

logger = hivemind.get_logger(__file__)

logger.info("Loading models")
models = utils.load_models()

logger.info("Starting Flask app")
app = Flask(__name__)
app.config["MODELS"] = models
CORS(app)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}

logger.info("Pre-rendering index page")
index_html = views.render_index(app)


@app.route("/")
def main_page():
    response = make_response(index_html)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


from pollen.features.images.image_api import bp as image_api_bp
app.register_blueprint(image_api_bp)
from pollen.features.status.status_api import bp as status_api_bp
app.register_blueprint(status_api_bp)
from pollen.features.chat.http_api import bp as http_api_bp
app.register_blueprint(http_api_bp)

# WebSocket route: import websocket_api so its @sock.route runs (registering the
# route on the sock blueprint) and inject models, THEN bind sock to the app.
# Order matters: init_app must run after the route is registered on sock.
from pollen.features.chat import websocket_api
websocket_api.set_models(models)
sock.init_app(app)
