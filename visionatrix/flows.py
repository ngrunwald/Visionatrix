import builtins
import concurrent.futures
import contextlib
import io
import json
import logging
import os
import random
import shutil
import time
import typing
import zipfile
from base64 import b64decode
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import UploadFile
from packaging.version import Version, parse

from . import _version, comfyui_class_info, db_queries, options
from .comfyui import get_node_class_mappings
from .etc import is_english
from .models import install_model
from .models_map import get_flow_models
from .nodes_helpers import get_node_value, set_node_value
from .pydantic_models import Flow

SECONDS_TO_CACHE_INSTALLED_FLOWS = 10
SECONDS_TO_CACHE_AVAILABLE_FLOWS = 3 * 60

LOGGER = logging.getLogger("visionatrix")
CACHE_AVAILABLE_FLOWS = {}
CACHE_INSTALLED_FLOWS = {
    "update_time": time.time() - (SECONDS_TO_CACHE_INSTALLED_FLOWS + 1),
    "flows": {},
    "flows_comfy": {},
}

SUPPORTED_OUTPUTS = {
    "SaveImage": "image",
    "VHS_VideoCombine": "video",
}


def get_available_flows(flows_comfy: dict[str, dict] | None = None) -> dict[str, Flow]:
    if flows_comfy is None:
        flows_comfy = {}
    else:
        flows_comfy.clear()

    flows_storage_urls = [url.strip() for url in options.FLOWS_URL.split(";") if url.strip()]
    if not flows_storage_urls:
        LOGGER.warning("'FLOWS_URL' is empty. Unable to get available flows.")
        return {}

    combined_flows = {}
    combined_flows_comfy = {}

    for flows_storage_url in flows_storage_urls:
        cache_entry = CACHE_AVAILABLE_FLOWS.get(flows_storage_url, {})
        current_time = time.time()
        cache_expired = current_time > cache_entry.get("update_time", 0) + SECONDS_TO_CACHE_AVAILABLE_FLOWS

        if cache_expired or not cache_entry:
            etag = cache_entry.get("etag", "")
            flows, flows_comfy_single, new_etag = fetch_flows_from_url_or_path(flows_storage_url, etag)
            if flows is not None:
                # Update cache_entry with new data
                cache_entry = {
                    "update_time": current_time,
                    "etag": new_etag,
                    "flows": flows,
                    "flows_comfy": flows_comfy_single,
                }
                CACHE_AVAILABLE_FLOWS[flows_storage_url] = cache_entry
            else:
                # Use existing cache_entry (even if expired)
                flows = cache_entry.get("flows", {})
                flows_comfy_single = cache_entry.get("flows_comfy", {})
        else:
            # Cache is valid, use cached data
            flows = cache_entry.get("flows", {})
            flows_comfy_single = cache_entry.get("flows_comfy", {})

        # Merge the flows into combined_flows
        for flow_name, flow_data in flows.items():
            if flow_name not in combined_flows:
                combined_flows[flow_name] = flow_data
                combined_flows_comfy[flow_name] = flows_comfy_single[flow_name]
            else:
                # Handle duplicate flow names, prefer the latest version
                existing_version = parse(combined_flows[flow_name].version)
                new_version = parse(flow_data.version)
                if new_version > existing_version:
                    combined_flows[flow_name] = flow_data
                    combined_flows_comfy[flow_name] = flows_comfy_single[flow_name]

    flows_comfy.update(combined_flows_comfy)
    return combined_flows


def fetch_flows_from_url_or_path(flows_storage_url: str, etag: str):
    r_flows = {}
    r_flows_comfy = {}
    if flows_storage_url.endswith("/"):
        vix_version = Version(_version.__version__)
        if vix_version.is_devrelease:
            flows_storage_url += "flows.zip"
        else:
            flows_storage_url += f"flows-{vix_version.major}.{vix_version.minor}.zip"
    parsed_url = urlparse(flows_storage_url)
    if parsed_url.scheme in ("http", "https", "ftp", "ftps"):
        try:
            r = httpx.get(flows_storage_url, headers={"If-None-Match": etag}, timeout=5.0)
        except httpx.TransportError as e:
            LOGGER.error("Request to get flows failed with: %s", str(e))
            return None, None, etag
        if r.status_code == 304:
            return None, None, etag
        if r.status_code != 200:
            LOGGER.error("Request to get flows returned: %s", r.status_code)
            return None, None, etag
        flows_content = r.content
        flows_content_etag = r.headers.get("etag", etag)
    else:
        try:
            with builtins.open(flows_storage_url, mode="rb") as flows_archive:
                flows_content = flows_archive.read()
            flows_content_etag = etag
        except Exception as e:
            LOGGER.error("Failed to read flows archive at %s: %s", flows_storage_url, str(e))
            return None, None, etag

    try:
        with zipfile.ZipFile(io.BytesIO(flows_content)) as zip_file:
            for flow_comfy_path in {name for name in zip_file.namelist() if name.endswith(".json")}:
                with zip_file.open(flow_comfy_path) as flow_comfy_file:
                    _flow_comfy = json.loads(flow_comfy_file.read())
                    _flow = get_vix_flow(_flow_comfy)
                    _flow_name = _flow.name.lower()
                    r_flows[_flow_name] = _flow
                    r_flows_comfy[_flow_name] = _flow_comfy
    except Exception as e:
        LOGGER.error("Failed to parse flows from %s: %s", flows_storage_url, str(e))
        return None, None, etag

    return r_flows, r_flows_comfy, flows_content_etag


def get_not_installed_flows(flows_comfy: dict[str, dict] | None = None) -> dict[str, Flow]:
    installed_flows_ids = list(get_installed_flows())
    avail_flows_comfy = {}
    avail_flows = get_available_flows(avail_flows_comfy)
    flows = {}
    for i, v in avail_flows.items():
        if i not in installed_flows_ids:
            flows[i] = v
            if flows_comfy is not None:
                flows_comfy[i] = avail_flows_comfy[i]
    return flows


def get_installed_flows(flows_comfy: dict[str, dict] | None = None) -> dict[str, Flow]:
    if flows_comfy is None:
        flows_comfy = {}
    else:
        flows_comfy.clear()
    if time.time() < CACHE_INSTALLED_FLOWS["update_time"] + SECONDS_TO_CACHE_INSTALLED_FLOWS:
        flows_comfy.update(CACHE_INSTALLED_FLOWS["flows_comfy"])
        return CACHE_INSTALLED_FLOWS["flows"]

    available_flows = get_available_flows({})
    public_flows_names = list(available_flows)
    CACHE_INSTALLED_FLOWS["update_time"] = time.time()
    flows = [entry for entry in Path(options.FLOWS_DIR).iterdir() if entry.is_file() and entry.name.endswith(".json")]
    r = {}
    r_comfy = {}
    for flow in flows:
        _flow_comfy = json.loads(flow.read_bytes())
        _flow_vix = get_vix_flow(_flow_comfy)
        if _flow_vix.name not in public_flows_names:
            _flow_vix.private = True
        _fresh_flow_info = available_flows.get(_flow_vix.name)
        if _fresh_flow_info and parse(_flow_vix.version) < parse(_fresh_flow_info.version):
            _flow_vix.new_version_available = _fresh_flow_info.version
        r[_flow_vix.name] = _flow_vix
        r_comfy[_flow_vix.name] = _flow_comfy
    CACHE_INSTALLED_FLOWS.update({"flows": r, "flows_comfy": r_comfy})
    if flows_comfy is not None:
        flows_comfy.update(r_comfy)
    return r


def get_installed_flow(flow_name: str, flow_comfy: dict[str, dict]) -> Flow | None:
    flows_comfy = {}
    flow = get_installed_flows(flows_comfy).get(flow_name)
    if flow:
        flow_comfy.clear()
        flow_comfy.update(flows_comfy[flow_name])
    return flow


def install_custom_flow(
    flow: Flow,
    flow_comfy: dict,
    progress_callback: typing.Callable[[str, float, str, bool], bool] | None = None,
) -> bool:
    uninstall_flow(flow.name)
    progress_for_model = 97 / max(len(flow.models), 1)
    if progress_callback is not None and not progress_callback(flow.name, 1.0, "", False):
        return False
    hf_auth_token = ""
    gated_models = [i for i in flow.models if i.gated]
    if gated_models and options.VIX_MODE != "SERVER":
        if "HF_AUTH_TOKEN" in os.environ:
            hf_auth_token = os.environ["HF_AUTH_TOKEN"]
        elif options.VIX_MODE == "DEFAULT":
            hf_auth_token = db_queries.get_global_setting("huggingface_auth_token", True)
        else:
            r = httpx.get(
                options.VIX_SERVER.rstrip("/") + "/setting",
                params={"key": "huggingface_auth_token"},
                auth=options.worker_auth(),
                timeout=float(options.WORKER_NET_TIMEOUT),
            )
            if not httpx.codes.is_error(r.status_code):
                hf_auth_token = r.text
        if not hf_auth_token:
            LOGGER.warning("Flow has gated model(s): %s; AccessToken was not found.", [i.name for i in gated_models])

    install_models_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=options.MAX_PARALLEL_DOWNLOADS) as executor:
        futures = [
            executor.submit(install_model, model, flow.name, progress_for_model, progress_callback, hf_auth_token)
            for model in flow.models
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                install_models_result = future.result()
                install_models_results.append(install_models_result)
            except Exception as e:
                LOGGER.exception("Error during models installation: %s", e)
                return False

    if not all(install_models_results):
        LOGGER.info("Installation of `%s` was unsuccessful", flow.name)
        return False

    local_flow_path = os.path.join(options.FLOWS_DIR, f"{flow.name}.json")
    if progress_callback is not None and not progress_callback(flow.name, 99.0, "", False):
        return False
    with builtins.open(local_flow_path, mode="w", encoding="utf-8") as fp:
        json.dump(flow_comfy, fp, indent=2)
    CACHE_INSTALLED_FLOWS["update_time"] = 0
    if progress_callback is None:
        return True
    return progress_callback(flow.name, 100.0, "", False)


def uninstall_flow(flow_name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(options.FLOWS_DIR, f"{flow_name}.json"))
    CACHE_INSTALLED_FLOWS["update_time"] = 0


def prepare_flow_comfy(
    flow: Flow,
    flow_comfy: dict,
    in_texts_params: dict,
    in_files_params: list[UploadFile | dict],
    task_details: dict,
) -> dict:
    r = deepcopy(flow_comfy)
    for i in [i for i in flow.input_params if i["type"] in ("text", "number", "list", "bool", "range", "range_scale")]:
        v = prepare_flow_comfy_get_input_value(in_texts_params, i)
        if v is None:
            continue
        for k, input_path in i["comfy_node_id"].items():
            node = r.get(k, {})
            if not node:
                raise RuntimeError(f"Bad workflow, node with id=`{k}` can not be found.")
            set_node_value(node, input_path, v)
    process_seed_value(flow, in_texts_params, r)
    prepare_flow_comfy_files_params(flow, in_files_params, task_details["task_id"], task_details, r)
    return r


def prepare_flow_comfy_get_input_value(in_texts_params: dict, i: dict) -> typing.Any:
    v = in_texts_params.get(i["name"], None)
    if v is None:
        if "default" in i:
            v = i["default"]
        elif not i.get("optional", False):
            raise RuntimeError(f"Missing `{i['name']}` parameter.")
        else:
            return None
    if i["type"] == "list":  # for `list` type we need associated values
        v = i["options"][v]
    elif i["type"] == "bool":
        if isinstance(v, str):
            v = int(v)
        v = bool(v)
    return v


def prepare_flow_comfy_files_params(
    flow: Flow, in_files_params: list[UploadFile | dict], task_id: int, task_details: dict, r: dict
) -> None:
    files_params = [i for i in flow.input_params if i["type"] in ("image", "image-mask", "video")]
    min_required_files_count = len([i for i in files_params if not i.get("optional", False)])
    if len(in_files_params) < min_required_files_count:
        raise RuntimeError(f"{len(in_files_params)} files given, but {min_required_files_count} at least required.")
    for i, v in enumerate(in_files_params):
        file_name = f"{task_id}_{i}"
        for k, input_path in files_params[i]["comfy_node_id"].items():
            node = r.get(k, {})
            if not node:
                raise RuntimeError(f"Bad workflow, node with id=`{k}` can not be found.")
            set_node_value(node, input_path, file_name)
        result_path = os.path.join(options.TASKS_FILES_DIR, "input", file_name)
        if isinstance(v, dict):
            if "input_index" in v:
                input_file = os.path.join(options.TASKS_FILES_DIR, "input", f"{v['task_id']}_{v['input_index']}")
                if not os.path.exists(input_file):
                    raise RuntimeError(
                        f"Bad flow, file from task_id=`{v['task_id']}`, index=`{v['input_index']}` not found."
                    )
                shutil.copy(input_file, result_path)
            elif "node_id" in v:
                input_file = ""
                result_prefix = f"{v['task_id']}_{v['node_id']}_"
                output_directory = os.path.join(options.TASKS_FILES_DIR, "output")
                for filename in os.listdir(output_directory):
                    if filename.startswith(result_prefix):
                        input_file = os.path.join(output_directory, filename)
                if not input_file or not os.path.exists(input_file):
                    raise RuntimeError(
                        f"Bad flow, file from task_id=`{v['task_id']}`, node_id={v['node_id']} not found."
                    )
                shutil.copy(input_file, result_path)
            else:
                raise RuntimeError("Bad flow, `input_index` or `node_id` should be present.")
        else:
            with builtins.open(result_path, mode="wb") as fp:
                v.file.seek(0)
                start_of_file = v.file.read(30)
                base64_index = start_of_file.find(b"base64,")
                if base64_index != -1:
                    v.file.seek(base64_index + len(b"base64,"))
                    fp.write(b64decode(v.file.read()))
                else:
                    v.file.seek(0)
                    shutil.copyfileobj(v.file, fp)
        task_details["input_files"].append({"file_name": file_name, "file_size": os.path.getsize(result_path)})
    for node_to_disconnect in files_params[len(in_files_params) :]:
        for node_id_to_disconnect in node_to_disconnect["comfy_node_id"]:
            disconnect_node_graph(node_id_to_disconnect, r)


def disconnect_node_graph(node_id: str, flow_comfy: dict[str, dict]) -> None:
    next_nodes_to_disconnect = []
    nodes_class_mappings = get_node_class_mappings()
    for next_node_id, next_node_details in flow_comfy.items():
        nodes_to_pop = []
        for input_id, input_details in next_node_details.get("inputs", {}).items():
            if isinstance(input_details, list) and input_details[0] == node_id:
                nodes_to_pop.append(input_id)
        for i in nodes_to_pop:
            class_type = next_node_details.get("class_type")
            if class_type is not None:
                node_class_mapping = nodes_class_mappings.get(class_type)
                if node_class_mapping is not None and hasattr(node_class_mapping, "INPUT_TYPES"):
                    next_node_input_types = node_class_mapping.INPUT_TYPES()
                    if "required" in next_node_input_types and i in next_node_input_types["required"]:
                        next_nodes_to_disconnect.append(next_node_id)
            next_node_details["inputs"].pop(i)
    flow_comfy.pop(node_id)
    for i in next_nodes_to_disconnect:
        disconnect_node_graph(i, flow_comfy)


def flow_prepare_output_params(
    outputs: list[str], task_id: int, task_details: dict, flow_comfy: dict[str, dict]
) -> None:
    for param in outputs:
        r_node = flow_comfy[param]
        if r_node["class_type"] in (
            "KSampler (Efficient)",
            "WD14Tagger|pysssss",
            "StringFunction|pysssss",
            "Evaluate Integers",
            "ShowText|pysssss",
            "MathExpression|pysssss",
            "PreviewImage",
        ):
            continue
        supported_outputs = SUPPORTED_OUTPUTS.keys()
        if r_node["class_type"] not in supported_outputs:
            raise RuntimeError(
                f"class_type={r_node['class_type']}: only {supported_outputs} nodes are supported currently as outputs"
            )
        r_node["inputs"]["filename_prefix"] = f"{task_id}_{param}"
        task_details["outputs"].append(
            {
                "comfy_node_id": int(param),
                "type": SUPPORTED_OUTPUTS[r_node["class_type"]],
                "file_size": -1,
                "batch_size": -1,
            }
        )


def process_seed_value(flow: Flow, in_texts_params: dict, flow_comfy: dict[str, dict]) -> None:
    if "seed" in [i["name"] for i in flow.input_params]:
        return  # skip automatic processing of "seed" if it was manually defined in "flow.json"
    random_seed = in_texts_params.get("seed", random.randint(1, 3999999999))
    for node_details in flow_comfy.values():
        if "inputs" in node_details:
            if "seed" in node_details["inputs"]:
                node_details["inputs"]["seed"] = random_seed
            elif (
                node_details["class_type"] in ("SamplerCustom", "RandomNoise", "KSamplerAdvanced")
                and "noise_seed" in node_details["inputs"]
            ):
                node_details["inputs"]["noise_seed"] = random_seed
    in_texts_params["seed"] = random_seed


def get_vix_flow(flow_comfy: dict[str, dict]) -> Flow:
    vix_flow = get_flow_metadata(flow_comfy)
    vix_flow["sub_flows"] = get_flow_subflows(flow_comfy)
    vix_flow["input_params"] = get_flow_inputs(flow_comfy)
    vix_flow["models"] = get_flow_models(flow_comfy)
    return Flow.model_validate(vix_flow)


def get_flow_metadata(flow_comfy: dict[str, dict]) -> dict[str, str | list | dict]:
    for node_details in flow_comfy.values():
        if node_details["class_type"] == "VixUiWorkflowMetadata":
            r = node_details["inputs"].copy()
            for i in ("tags", "requires"):
                if value := node_details["inputs"].get(i):
                    r[i] = json.loads(value)
            return r
        if node_details.get("_meta", {}).get("title", "") == "WF_META":  # Text Multiline (Code Compatible)
            return json.loads(node_details["inputs"]["text"])
    raise ValueError("ComfyUI flow should contain Workflow metadata")


def get_flow_subflows(flow_comfy: dict[str, dict]) -> list[dict[str, str | list | dict]]:
    for node_details in flow_comfy.values():
        if node_details.get("_meta", {}).get("title", "") == "WF_SUBFLOWS":
            return json.loads(node_details["inputs"]["text"])
    return []


def get_flow_inputs(flow_comfy: dict[str, dict]) -> list[dict[str, str | list | dict]]:
    input_params = []
    for node_id, node_details in flow_comfy.items():
        class_type = str(node_details["class_type"])
        image_mask = False
        if class_type.startswith("VixUi"):
            if node_details["class_type"] == "VixUiWorkflowMetadata":
                continue
            display_name = node_details["inputs"]["display_name"]
            other_attributes = ()
            optional = node_details["inputs"]["optional"]
            advanced = node_details["inputs"]["advanced"]
            order = node_details["inputs"]["order"]
            custom_id = node_details["inputs"]["custom_id"]
            hidden_attribute = node_details["inputs"].get("hidden", False)
            translatable = node_details["inputs"].get("translatable", False)
        elif node_details["_meta"]["title"].startswith("input;"):
            input_info = str(node_details["_meta"]["title"]).split(";")
            input_info = [i.strip() for i in input_info]
            display_name = input_info[1]
            other_attributes = tuple(s.lower() for s in input_info[2:])
            optional = bool("optional" in other_attributes)
            advanced = bool("advanced" in other_attributes)
            translatable = bool("translatable" in other_attributes)
            order = 20 if class_type == "SDXLAspectRatioSelector" else 99
            for attribute in other_attributes:
                if attribute.startswith("order="):
                    order = int(attribute[6:])
                    break
            custom_id = ""
            for attribute in other_attributes:
                if attribute.startswith("custom_id="):
                    custom_id = attribute[10:]
                    break
            hidden_attribute = bool("hidden" in other_attributes)
            image_mask = bool("mask" in other_attributes)
        else:
            continue
        try:
            input_type, input_path = comfyui_class_info.CLASS_INFO[node_details["class_type"]]
            if image_mask is True and input_type == "image":
                input_type = "image-mask"
        except KeyError as exc:
            raise ValueError(
                f"Node with class_type={node_details['class_type']} is not currently supported as input"
            ) from exc

        input_param_data = {
            "name": custom_id if custom_id else f"in_param_{node_id}",
            "display_name": display_name,
            "type": input_type,
            "optional": optional,
            "advanced": advanced,
            "default": get_node_value(node_details, input_path),
            "order": order,
            "comfy_node_id": {node_id: input_path},
            "hidden": hidden_attribute,
            "translatable": translatable,
        }
        if image_mask:
            for attribute in other_attributes:
                if attribute.startswith("source_input_name="):
                    input_param_data["source_input_name"] = attribute[18:]
                    break
            if "source_input_name" not in input_param_data:
                raise ValueError("`source_input_name` required for mask parameter.")
        if node_details["class_type"] in ("VixUiRangeFloat", "VixUiRangeScaleFloat", "VixUiRangeInt"):
            for ex_input in ("min", "max", "step"):
                input_param_data[ex_input] = node_details["inputs"][ex_input]
            if node_details["class_type"] == "VixUiRangeScaleFloat" and "source_input_name" in node_details["inputs"]:
                input_param_data["source_input_name"] = node_details["inputs"]["source_input_name"]
        elif node_details["class_type"] in ("VixUiList", "VixUiListLogic"):
            r = json.loads(node_details["inputs"]["possible_values"])
            if isinstance(r, list):
                input_param_data["options"] = {i: i for i in r}
            else:
                input_param_data["options"] = r
        elif class_type == "SDXLAspectRatioSelector":
            correct_aspect_ratio_default_options(input_param_data)
        input_params.append(input_param_data)
    return sorted(input_params, key=lambda x: x["order"])


def correct_aspect_ratio_default_options(input_param_data: dict) -> None:
    _options = {
        "1:1 (1024x1024)": "1:1",
        "2:3 (832x1216)": "2:3",
        "3:4 (896x1152)": "3:4",
        "5:8 (768x1216)": "5:8",
        "9:16 (768x1344)": "9:16",
        "9:19 (704x1472)": "9:19",
        "9:21 (640x1536)": "9:21",
        "3:2 (1216x832)": "3:2",
        "4:3 (1152x896)": "4:3",
        "8:5 (1216x768)": "8:5",
        "16:9 (1344x768)": "16:9",
        "19:9 (1472x704)": "19:9",
        "21:9 (1536x640)": "21:9",
    }
    input_param_data["options"] = _options
    input_param_data["default"] = [i for i in _options if i.find(input_param_data["default"]) != -1][0]  # noqa


def get_ollama_nodes(flow_comfy: dict) -> list[str]:
    r = []
    for node_id, node_details in flow_comfy.items():
        if str(node_details["class_type"]) in ("OllamaVision", "OllamaGenerate", "OllamaGenerateAdvance"):
            r.append(node_id)
    return r


def get_google_nodes(flow_comfy: dict) -> list[str]:
    r = []
    for node_id, node_details in flow_comfy.items():
        if str(node_details["class_type"]) == "Gemini_Flash":
            r.append(node_id)
    return r


def get_nodes_for_translate(input_params: dict[str, typing.Any], flow_comfy: dict) -> list[dict[str, typing.Any]]:
    r = []
    for input_param, input_param_value in input_params.items():
        if input_param.startswith("in_param_"):
            node_info = flow_comfy[input_param[len("in_param_") :]]
        else:
            node_info = None
            for node_value in flow_comfy.values():
                if node_value.get("inputs", {}).get("custom_id", "") == input_param:
                    node_info = node_value
                    break
            if not node_info:
                if input_param != "seed":
                    LOGGER.warning("Can not find node for `%s` input param.", input_param)
                continue
        if node_info.get("inputs", {}).get("translatable", False) and not is_english(input_param_value):
            r.append(
                {
                    "input_param_id": input_param,
                    "input_param_value": input_param_value,
                    "llm_prompt": "",
                }
            )
    return r
