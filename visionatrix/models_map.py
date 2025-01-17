import builtins
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from . import options
from .basic_node_list import BASIC_NODE_LIST
from .comfyui import get_node_class_mappings
from .nodes_helpers import get_node_value, set_node_value
from .pydantic_models import AIResourceModel

LOGGER = logging.getLogger("visionatrix")
MODEL_LOAD_CLASSES = {
    "CheckpointLoaderSimple": {
        "1": ["inputs", "ckpt_name"],
    },
    "ControlNetLoader": {
        "1": ["inputs", "control_net_name"],
    },
    "Efficient Loader": {
        "1": ["inputs", "ckpt_name"],
        "2": ["inputs", "lora_name"],
    },
    "InstantIDModelLoader": {"1": ["inputs", "instantid_file"]},
    "IPAdapterUnifiedLoader": {"preset": ["inputs", "preset"]},
    "LoraLoader": {"1": ["inputs", "lora_name"]},
    "LoraLoaderModelOnly": {"1": ["inputs", "lora_name"]},
    "PhotoMakerLoader": {"1": ["inputs", "photomaker_model_name"]},
    "VAELoader": {"1": ["inputs", "vae_name"]},
    "UpscaleModelLoader": {"1": ["inputs", "model_name"]},
    "SUPIR_model_loader_v2": {"1": ["inputs", "supir_model"]},
    "PulidModelLoader": {"1": ["inputs", "pulid_file"]},
    "Lora Loader Stack (rgthree)": {
        "1": ["inputs", "lora_01"],
        "2": ["inputs", "lora_02"],
        "3": ["inputs", "lora_03"],
        "4": ["inputs", "lora_04"],
    },
    "UNETLoader": {"1": ["inputs", "unet_name"]},
    "DualCLIPLoader": {
        "1": ["inputs", "clip_name1"],
        "2": ["inputs", "clip_name2"],
    },
    "PhotoMakerLoaderPlus": {
        "1": ["inputs", "photomaker_model_name"],
    },
    "RIFE VFI": {
        "preset": ["inputs", "ckpt_name"],
    },
    "DWPreprocessor": {
        "preset1": ["inputs", "bbox_detector"],
        "preset2": ["inputs", "pose_estimator"],
    },
}
MODELS_CATALOG: dict[str, dict] = {}


def get_flow_models(flow_comfy: dict[str, dict]) -> list[AIResourceModel]:
    nodes_with_models = {key: value["models"] for key, value in BASIC_NODE_LIST.items() if value.get("models")}
    nodes_class_mappings = get_node_class_mappings()

    models_catalog = get_models_catalog()
    models_info: list[AIResourceModel] = []
    for node_details in flow_comfy.values():
        class_type = node_details.get("class_type")
        node_class_mapping = nodes_class_mappings.get(class_type)
        if node_class_mapping and hasattr(node_class_mapping, "RELATIVE_PYTHON_MODULE"):
            node_module = str(node_class_mapping.RELATIVE_PYTHON_MODULE).rsplit(".", 1)[-1]
            models_from_nodes = nodes_with_models.get(node_module, [])
            for node_model_info in models_from_nodes:
                if node_model_info.name not in [i.name for i in models_info]:
                    models_info.append(node_model_info)

        if (load_class := MODEL_LOAD_CLASSES.get(class_type)) is None:
            continue
        for k, node_model_load_path in load_class.items():
            node_input_model_name = get_node_value(node_details, node_model_load_path)
            if node_input_model_name == "None":
                continue
            not_found = True
            for model, model_details in models_catalog.items():
                if match_replace_model(model_details, node_input_model_name, node_details, node_model_load_path, k):
                    if model not in [i.name for i in models_info]:
                        models_info.append(AIResourceModel(**model_details, name=model))
                    not_found = False
            if not_found:
                LOGGER.error(
                    "Can not map model(%s) for %s(%s):\n%s", node_input_model_name, class_type, k, node_details
                )
    return models_info


def match_replace_model(
    model_details: dict,
    node_input_model_name: str,
    node_details: dict,
    node_model_load_path: list[str],
    node_model_load_path_key: str,
) -> bool:
    for regex in model_details["regexes"]:
        _input_value = "input_value" not in regex or re.match(regex["input_value"], node_input_model_name) is not None
        _input_name = "input_name" not in regex or re.match(regex["input_name"], node_model_load_path[-1]) is not None
        _class_name = "class_name" not in regex or re.match(regex["class_name"], node_details["class_type"]) is not None
        if _input_value and _input_name and _class_name:
            if not node_model_load_path_key.lower().startswith("preset"):
                set_node_value(
                    node_details,
                    node_model_load_path,
                    skip_first_part_of_path(model_details["save_path"]),
                )
            return True
    return False


def skip_first_part_of_path(save_path: str):
    parts = save_path.split("/", 1)
    return parts[1] if len(parts) > 1 else save_path


def get_models_catalog() -> dict[str, dict]:
    if not MODELS_CATALOG:
        if urlparse(options.MODELS_CATALOG_URL).scheme in ("http", "https", "ftp", "ftps"):
            MODELS_CATALOG.update(json.loads(httpx.get(options.MODELS_CATALOG_URL, timeout=5.0).text))
        else:
            with builtins.open(options.MODELS_CATALOG_URL, encoding="UTF-8") as models_catalog_file:
                MODELS_CATALOG.update(json.loads(models_catalog_file.read()))
    return MODELS_CATALOG


def get_formatted_models_catalog() -> list[AIResourceModel]:
    r = []
    for model, model_details in get_models_catalog().items():
        r.append(AIResourceModel(**model_details, name=model))
    return r
