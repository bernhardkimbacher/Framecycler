import os

# Set default shipped OCIO config if not specified by user
if "OCIO" not in os.environ:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_config = os.path.join(current_dir, "color", "studio_config", "config.ocio")
    if os.path.exists(bundled_config):
        os.environ["OCIO"] = bundled_config
