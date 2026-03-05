import dataclasses
import json

from flask import Flask, render_template

import config
import image_gen


def render_index(app: Flask) -> str:
    default_family = list(config.MODEL_FAMILIES.values())[0]
    default_model = list(default_family)[0]

    model_configs = {
        model_config.backend.key: model_config
        for family_models in config.MODEL_FAMILIES.values()
        for model_config in family_models
    }

    with app.app_context():
        return render_template(
            "index.html",
            default_model=default_model,
            model_families=config.MODEL_FAMILIES,
            model_config_json=json.dumps(model_configs, default=dataclasses.asdict),
            app_name=config.APP_NAME,
            model_display_name=config.MODEL_DISPLAY_NAME,
            model_badge=config.MODEL_BADGE,
            welcome_message=config.WELCOME_MESSAGE,
            show_petals_branding=config.SHOW_PETALS_BRANDING,
            image_enabled=image_gen.is_enabled(),
        )
