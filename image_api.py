"""Flask API endpoint for image generation."""

import logging
import threading

from flask import jsonify, request, send_from_directory

from app import app
import image_gen

log = logging.getLogger("image_api")


@app.route("/api/generate-image", methods=["POST"])
def api_generate_image():
    if not image_gen.is_enabled():
        return jsonify(ok=False, error="Image generation is disabled"), 503

    prompt = (request.json or {}).get("prompt", "").strip() if request.is_json else request.form.get("prompt", "").strip()
    if not prompt:
        return jsonify(ok=False, error="No prompt provided"), 400
    if len(prompt) > 500:
        return jsonify(ok=False, error="Prompt too long (max 500 chars)"), 400

    try:
        result = image_gen.generate_image(prompt)
        return jsonify(
            ok=True,
            filename=result["filename"],
            url=f"/static/generated/{result[filename]}",
            elapsed=result["elapsed"],
        )
    except Exception as e:
        log.error("Image generation failed: %s", e, exc_info=True)
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/image-status")
def api_image_status():
    return jsonify(
        enabled=image_gen.is_enabled(),
        loading=image_gen.is_loading(),
        ready=image_gen.is_ready(),
        model=image_gen.IMAGE_MODEL,
        device=image_gen.IMAGE_DEVICE,
    )


@app.route("/static/generated/<filename>")
def serve_generated_image(filename):
    """Serve generated images from the output directory."""
    return send_from_directory(image_gen.IMAGE_OUTPUT_DIR, filename)
