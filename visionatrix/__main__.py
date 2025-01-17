import argparse
import builtins
import importlib.resources
import json
import logging
import os
import sys
from fnmatch import fnmatchcase
from pathlib import Path

from . import comfyui, database, install, options, run_vix, update
from .etc import get_higher_log_level, get_log_level
from .flows import get_not_installed_flows, get_vix_flow, install_custom_flow
from .install_update import flow_install_callback
from .orphan_models import process_orphan_models

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    default_log_level = os.environ.get("LOG_LEVEL", "INFO")
    parser.add_argument(
        "--loglevel",
        type=str,
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        default=default_log_level,
    )
    subparsers = parser.add_subparsers(dest="command")
    for i in [
        ("install", "Performs cleanup & initialization"),
        ("update", "Performs update to the latest version"),
        ("run", "Starts the ComfyUI and Visionatrix backends"),
        ("install-flow", "Install flow by name or from file"),
        ("create-user", "Create new user"),
        ("orphan-models", "Remove orphan models"),
    ]:
        subparser = subparsers.add_parser(i[0], help=i[1])
        if i[0] == "create-user":
            subparser.add_argument("--name", type=str, help="User name(ID)", required=True)
            subparser.add_argument("--password", type=str, help="User password", required=True)
            subparser.add_argument("--full_name", type=str, help="Full User Name", default="John Doe")
            subparser.add_argument("--email", type=str, help="User's email address", default="user@example.com")
            subparser.add_argument("--admin", type=bool, help="Should user be admin", default=True)
            subparser.add_argument("--disabled", type=bool, help="Should account be disabled", default=False)
            continue

        if i[0] == "orphan-models":
            subparser.add_argument(
                "--no-confirm", action="store_true", help="Do not ask for confirmation for each model"
            )
            subparser.add_argument(
                "--dry-run", action="store_true", help="Perform cleaning without actual removing models"
            )
            subparser.add_argument(
                "--include-useful-models",
                action="store_true",
                help="Include orphaned models that can be used in future flows for removal.",
            )

        if i[0] == "install-flow":
            install_flow_group = subparser.add_mutually_exclusive_group(required=True)
            install_flow_group.add_argument("--file", type=str, help="Path to `comfyui_flow.json` file")
            install_flow_group.add_argument("--name", type=str, help="Flow name mask of the flow(s)")
            install_flow_group.add_argument("--tag", type=str, help="Flow tags mask of the flow(s)")

        subparser.add_argument("--backend_dir", type=str, help="Directory for the backend")
        subparser.add_argument("--flows_dir", type=str, help="Directory for the flows")
        subparser.add_argument("--models_dir", type=str, help="Directory for the models")
        if i[0] == "run":
            subparser.add_argument("--host", type=str, help="Host to listen (DEFAULT or SERVER mode)")
            subparser.add_argument("--port", type=str, help="Port to listen (DEFAULT or SERVER mode)")
            subparser.add_argument("--server", type=str, help="Address of Vix Server(WORKER mode)")
            subparser.add_argument("--tasks_files_dir", type=str, help="Directory for input/output files")
            subparser.add_argument("--mode", choices=["WORKER", "SERVER"], help="VIX special operating mode")
            subparser.add_argument("--ui", nargs="?", default="", help="Enable WebUI (DEFAULT or SERVER mode)")
            subparser.add_argument("--disable-device-detection", action="store_true", default=False)
            comfyui.add_arguments(subparser)

    args = parser.parse_args()
    defined_loglvl = get_log_level(args.loglevel)
    logging.basicConfig(
        level=defined_loglvl,
        format="%(asctime)s: [%(funcName)s]:%(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(get_higher_log_level(defined_loglvl))
    if args.command == "run":
        if args.host:
            options.VIX_HOST = args.host
        if args.port:
            options.VIX_PORT = args.port
        if args.ui is None:  # `--ui`: enable default UI
            options.UI_DIR = str(importlib.resources.files("visionatrix").joinpath("client"))
        elif args.ui != "":
            options.UI_DIR = args.ui
        if args.mode:
            options.VIX_MODE = args.mode
        if args.server:
            options.VIX_SERVER = args.server
    if options.VIX_MODE != "WORKER" or not options.VIX_SERVER:  # we get tasks directly from the Database
        database.init_database_engine()
    if args.command == "create-user":
        database.create_user(args.name, args.full_name, args.email, args.password, args.admin, args.disabled)
        sys.exit(0)
    options.init_dirs_values(
        backend=getattr(args, "backend_dir", ""),
        flows=getattr(args, "flows_dir", ""),
        models=getattr(args, "models_dir", ""),
        tasks_files=getattr(args, "tasks_files_dir", ""),
    )
    if args.command == "install":
        operations_mask = [True, True, True]
        if Path(options.MODELS_DIR).exists():
            c = input("Do you want to clear models folder? (Y/N): ").lower()
            if c != "y":
                operations_mask[2] = False
        if Path(options.FLOWS_DIR).exists():
            c = input("Do you want to clear flows folder? (Y/N): ").lower()
            if c != "y":
                operations_mask[1] = False
        if Path(options.BACKEND_DIR).exists():
            c = input("Do you want to reinstall backend(ComfyUI) folder? (Y/N): ").lower()
            if c != "y":
                operations_mask[0] = False
        install(operations_mask)
    elif args.command == "update":
        update()
    elif args.command == "run":
        run_vix()
    elif args.command == "install-flow":
        comfyui.load(None)
        if args.file:
            with builtins.open(Path(args.file), "rb") as fp:
                install_flow_comfy = json.loads(fp.read())
            install_custom_flow(
                flow=get_vix_flow(install_flow_comfy),
                flow_comfy=install_flow_comfy,
                progress_callback=flow_install_callback.progress_callback,
            )
        else:
            flows_comfy = {}
            not_installed_flows = get_not_installed_flows(flows_comfy)
            if args.tag:
                flow_install_pattern = str(args.tag)
                flows_to_install = {}
                for flow_name, flow in not_installed_flows.items():
                    if any(fnmatchcase(tag, flow_install_pattern) for tag in flow.tags):
                        flows_to_install[flow_name] = flow
            else:
                flow_install_pattern = str(args.name).lower()
                flows_to_install = {
                    name: flow for name, flow in not_installed_flows.items() if fnmatchcase(name, flow_install_pattern)
                }
            if not flows_to_install:
                logging.getLogger("visionatrix").error("No flows found matching pattern: '%s'", flow_install_pattern)
                sys.exit(2)
            if len(flows_to_install) > 1:
                logging.getLogger("visionatrix").warning("Multiple flows match pattern: '%s'", flow_install_pattern)
                for flow_name, flow in flows_to_install.items():
                    logging.getLogger("visionatrix").warning(" - %s (tags: %s)", flow_name, ", ".join(flow.tags))
                confirm = input("Do you want to install all of them? (Y/N): ").lower()
                if confirm != "y":
                    logging.getLogger("visionatrix").info("Aborting installation.")
                    sys.exit(0)
            for flow_name, flow in flows_to_install.items():
                install_custom_flow(
                    flow=flow,
                    flow_comfy=flows_comfy[flow_name],
                    progress_callback=flow_install_callback.progress_callback,
                )
    elif args.command == "orphan-models":
        comfyui.load(None)
        process_orphan_models(args.dry_run, args.no_confirm, args.include_useful_models)
    else:
        logging.getLogger("visionatrix").error("Unknown command: '%s'", args.command)
        sys.exit(2)
    sys.exit(0)
