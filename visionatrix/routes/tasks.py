import builtins
import json
import logging
import os
import shutil
import typing
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    responses,
    status,
)

from .. import etc, options
from ..db_queries import get_setting
from ..db_queries_async import get_setting_async
from ..flows import (
    Flow,
    flow_prepare_output_params,
    get_installed_flow,
    get_nodes_for_translate,
    prepare_flow_comfy,
)
from ..prompt_translation import (
    translate_prompt_with_gemini,
    translate_prompt_with_gemini_async,
    translate_prompt_with_ollama,
    translate_prompt_with_ollama_async,
)
from ..pydantic_models import (
    TaskRunResults,
    TaskUpdateRequest,
    TranslatePromptRequest,
    UserInfo,
    WorkerDetailsRequest,
)
from ..tasks_engine import (
    TaskDetails,
    TaskDetailsShort,
    collect_child_task_ids,
    create_new_task,
    get_incomplete_task_without_error_database,
    get_task,
    get_task_files,
    get_tasks,
    get_tasks_short,
    put_task_in_queue,
    remove_task_by_id_database,
    remove_task_files,
    remove_task_lock_database,
    remove_unfinished_task_by_id,
    remove_unfinished_tasks_by_name_and_group,
    task_restart_database,
    update_task_info_database,
    update_task_outputs,
    update_task_progress_database,
)
from ..tasks_engine_async import (
    create_new_task_async,
    get_incomplete_task_without_error_database_async,
    get_task_async,
    get_tasks_async,
    get_tasks_short_async,
    put_task_in_queue_async,
    task_restart_database_async,
    update_task_info_database_async,
    update_task_outputs_async,
    update_task_progress_database_async,
)

LOGGER = logging.getLogger("visionatrix")
ROUTER = APIRouter(prefix="/tasks", tags=["tasks"])
VALIDATE_PROMPT: typing.Callable[[dict], tuple[bool, dict, list, list]] | None = None


async def __task_run(
    name: str,
    input_params: dict,
    translated_input_params: dict,
    in_files: list[UploadFile | dict],
    flow: Flow,
    flow_comfy: dict,
    user_info: UserInfo,
    webhook_url: str | None,
    webhook_headers: dict | None,
    child_task: bool,
    group_scope: int,
    priority: int,
):
    if options.VIX_MODE == "SERVER":
        task_details = await create_new_task_async(name, input_params, user_info)
    else:
        task_details = create_new_task(name, input_params, user_info)
    input_params_copy = input_params.copy()
    for i, v in translated_input_params.items():
        input_params_copy[i] = v
    try:
        flow_comfy = prepare_flow_comfy(flow, flow_comfy, input_params_copy, in_files, task_details)
    except RuntimeError as e:
        remove_task_files(task_details["task_id"], ["input"])
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None

    flow_validation: [bool, dict, list, list] = VALIDATE_PROMPT(flow_comfy)
    if not flow_validation[0]:
        remove_task_files(task_details["task_id"], ["input"])
        LOGGER.error("Flow validation error: %s\n%s", flow_validation[1], flow_validation[3])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Bad Flow: `{flow_validation[1]}`"
        ) from None
    task_details["flow_comfy"] = flow_comfy
    task_details["webhook_url"] = webhook_url
    task_details["webhook_headers"] = webhook_headers
    if child_task:
        if not in_files or not isinstance(in_files[0], dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No input file provided. A child task can only be created from the node ID of the parent task.",
            ) from None
        task_details["parent_task_id"] = in_files[0]["task_id"]
        task_details["parent_task_node_id"] = in_files[0]["node_id"]
    task_details["group_scope"] = group_scope
    task_details["priority"] = ((group_scope - 1) << 4) + priority
    if translated_input_params:
        task_details["translated_input_params"] = translated_input_params
    flow_prepare_output_params(flow_validation[2], task_details["task_id"], task_details, flow_comfy)
    if options.VIX_MODE == "SERVER":
        await put_task_in_queue_async(task_details)
    else:
        put_task_in_queue(task_details)
    return task_details


async def __get_translated_input_params(
    translate: bool, flow: Flow, input_params_dict: dict, flow_comfy: dict, user_id: str, is_user_admin: bool
):
    translated_input_params_dict = {}
    if translate and flow.is_translations_supported:
        nodes_for_translate = get_nodes_for_translate(input_params_dict, flow_comfy)
        if not nodes_for_translate:
            return translated_input_params_dict
        if options.VIX_MODE == "SERVER":
            translations_provider = await get_setting_async(user_id, "translations_provider", is_user_admin)
        else:
            translations_provider = get_setting(user_id, "translations_provider", is_user_admin)
        if translations_provider:
            if translations_provider not in ("ollama", "gemini"):
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail=f"Unknown translation provider: {translations_provider}",
                )
            for node_to_translate in nodes_for_translate:
                tr_req = TranslatePromptRequest(prompt=node_to_translate["input_param_value"])
                if node_to_translate["llm_prompt"]:
                    tr_req.system_prompt = node_to_translate["llm_prompt"]
                try:
                    if translations_provider == "ollama":
                        if options.VIX_MODE == "SERVER":
                            r = await translate_prompt_with_ollama_async(user_id, is_user_admin, tr_req)
                        else:
                            r = translate_prompt_with_ollama(user_id, is_user_admin, tr_req)
                    else:
                        if options.VIX_MODE == "SERVER":
                            r = await translate_prompt_with_gemini_async(user_id, is_user_admin, tr_req)
                        else:
                            r = translate_prompt_with_gemini(user_id, is_user_admin, tr_req)
                except Exception as e:
                    LOGGER.exception(
                        "Exception during prompt translation using `%s` for user `%s`", translations_provider, user_id
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Can't translate the prompt: provider={translations_provider}, "
                        f"user_id={user_id}, prompt=`{tr_req.prompt}`: {e}",
                    ) from None
                translated_input_params_dict[node_to_translate["input_param_id"]] = r.result
    return translated_input_params_dict


@ROUTER.put("/create")
async def create_task(
    request: Request,
    name: str = Form(description="Name of the flow from which the task should be created"),
    count: int = Form(1, description="Number of tasks to be created"),
    input_params: str = Form(None, description="List of input parameters as an encoded json string"),
    webhook_url: str | None = Form(None, description="URL to call when task state changes"),
    webhook_headers: str | None = Form(None, description="Headers for webhook url as an encoded json string"),
    child_task: int = Form(0, description="Int boolean indicating whether to create a relation between tasks"),
    group_scope: int = Form(1, description="Group number to which task should be assigned. Maximum value is 255."),
    priority: int = Form(
        0,
        description="Task execution priority. Higher numbers indicate higher priority. Maximum value is 15.",
    ),
    translate: int = Form(0, description="Should the prompt be translated if auto-translation option is enabled."),
    files: list[UploadFile | str] = Form(None, description="List of input files for flow"),  # noqa
) -> TaskRunResults:
    """
    Endpoint to initiate the creation and execution of tasks within the Vix workflow environment,
    handling both file inputs and task-related parameters.
    """

    if group_scope < 1 or group_scope > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Group number should from 1 to 255"
        ) from None

    if priority > 15:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Priority cannot be greater than 15.")

    user_id = request.scope["user_info"].user_id
    is_user_admin = request.scope["user_info"].is_admin
    in_files = []
    for i in files if files else []:
        if isinstance(i, str):
            try:
                input_file_info = json.loads(i)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid files input:{i}"
                ) from None
            if "task_id" not in input_file_info:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Missing `task_id` parameter"
                ) from None
            if not get_task(int(input_file_info["task_id"]), user_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Missing task with id={input_file_info['task_id']}",
                ) from None
            in_files.append(input_file_info)
        else:
            in_files.append(i)
    try:
        input_params_dict = json.loads(input_params) if input_params else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for params") from None
    if "seed" in input_params_dict:
        input_params_dict["seed"] = int(input_params_dict["seed"])

    flow_comfy = {}
    flow = get_installed_flow(name, flow_comfy)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Flow `{name}` is not installed.") from None

    translated_input_params_dict = await __get_translated_input_params(
        bool(translate), flow, input_params_dict, flow_comfy, user_id, is_user_admin
    )
    tasks_ids = []
    webhook_headers_dict = json.loads(webhook_headers) if webhook_headers else None
    for _ in range(count):
        task_details = await __task_run(
            name,
            input_params_dict,
            translated_input_params_dict,
            in_files,
            flow,
            flow_comfy,
            request.scope["user_info"],
            webhook_url,
            webhook_headers_dict,
            bool(child_task),
            group_scope,
            priority,
        )
        tasks_ids.append(task_details["task_id"])
        if "seed" in input_params_dict:
            input_params_dict["seed"] = input_params_dict["seed"] + 1
    try:
        return TaskRunResults.model_validate({"tasks_ids": tasks_ids})
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data validation error: {e}") from None


@ROUTER.get("/progress")
async def get_tasks_progress(
    request: Request,
    name: str = Query(None, description="Optional name to filter tasks by their name"),
    group_scope: int = Query(1, description="Optional parameter to filter tasks by their group number"),
    only_parent: bool = Query(False, description="Fetch only parent tasks"),
) -> dict[int, TaskDetails]:
    """
    Retrieves the full tasks details information for a specific user.
    Optionally filter tasks by their name or a group number.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_tasks_async(
            name=name,
            group_scope=group_scope,
            user_id=request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=only_parent,
        )
    else:
        r = get_tasks(
            name=name,
            group_scope=group_scope,
            user_id=request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=only_parent,
        )
    return r


@ROUTER.get("/progress-summary")
async def get_tasks_progress_summary(
    request: Request,
    name: str = Query(None, description="Optional name to filter tasks by their name"),
    group_scope: int = Query(1, description="Optional parameter to filter tasks by their group number"),
    only_parent: bool = Query(False, description="Fetch only parent tasks"),
) -> dict[int, TaskDetailsShort]:
    """
    Retrieves summary of the tasks progress details for a specific user.
    Optionally filter tasks by their name or a group number.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_tasks_short_async(
            name=name,
            group_scope=group_scope,
            user_id=request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=only_parent,
        )
    else:
        r = get_tasks_short(
            name=name,
            group_scope=group_scope,
            user_id=request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=only_parent,
        )
    return r


@ROUTER.get("/progress/{task_id}")
async def get_task_progress(request: Request, task_id: int) -> TaskDetails:
    """
    Retrieves the full task details of a specified task by task ID.
    Access is restricted to the task owner or an administrator.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id, request.scope["user_info"].user_id, fetch_child=True)
    else:
        r = get_task(task_id, request.scope["user_info"].user_id, fetch_child=True)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    try:
        return TaskDetails.model_validate(r)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data validation error: {e}") from None


@ROUTER.post(
    "/restart",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully restarted the specified task"},
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "examples": {
                        "Task already finished": {
                            "summary": "Task already finished",
                            "value": {"detail": "Task `{task_id}` already finished."},
                        },
                        "No error set": {
                            "summary": "No error set",
                            "value": {"detail": "Task `{task_id}` has no error set."},
                        },
                    }
                }
            },
        },
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def restart_task(
    request: Request,
    task_id: int = Query(..., description="ID of the task to restart"),
    force: bool = Query(False, description="Force restart even if the task has no error"),
):
    """
    Restarts a task specified by `task_id` if it has encountered an error or is not yet completed.
    Only tasks that have errors can be restarted unless `force` is set to `True`,
    which allows restarting any non-completed tasks.
    This endpoint checks the task's current status and resets its progress, allowing it to be re-executed.
    Access to this action is restricted to the task's owner or an administrator.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id, request.scope["user_info"].user_id)
    else:
        r = get_task(task_id, request.scope["user_info"].user_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if r["progress"] == 100.0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Task `{task_id}` already finished.")
    if not r["error"] and not force:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Task `{task_id}` has no error set.")

    if options.VIX_MODE == "SERVER":
        await task_restart_database_async(task_id)
    else:
        task_restart_database(task_id)
    remove_task_lock_database(task_id)


@ROUTER.delete(
    "/task",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully removed the specified task and its child tasks"},
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def delete_task(request: Request, task_id: int = Query(..., description="ID of the task to remove")):
    """
    Removes a task from the system by the task ID.
    Access is limited to the task owner or administrators.
    Also removes any child tasks associated with the specified task.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id, fetch_child=True)
    else:
        r = get_task(task_id, fetch_child=True)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Task `{task_id}` was not found.")
    if r["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=404, detail=f"Task `{task_id}` was not found.")
    task_ids_to_remove = [task_id]
    collect_child_task_ids(r, task_ids_to_remove)
    remove_task_by_id_database(task_ids_to_remove)


@ROUTER.delete(
    "/clear",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully removed results of all finished parent tasks with the specified name"},
    },
)
async def clear_tasks(
    request: Request,
    name: str = Query(..., description="Name of the task whose results need to be deleted"),
    group_scope: int = Query(
        1, description="Optional group scope to filter tasks only belonging to a specific group. Defaults to 1."
    ),
):
    """
    Removes all finished parent tasks associated with a specific task name,
    scoped to the requesting user and group scope.
    All child tasks associated with the parent tasks will also be deleted.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_tasks_async(
            name,
            group_scope,
            True,
            request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=True,
        )
    else:
        r = get_tasks(
            name,
            group_scope,
            True,
            request.scope["user_info"].user_id,
            fetch_child=True,
            only_parent=True,
        )
    task_ids_to_remove = []
    for task_id, task_details in r.items():
        task_ids_to_remove.append(task_id)
        collect_child_task_ids(task_details, task_ids_to_remove)
    remove_task_by_id_database(task_ids_to_remove)


@ROUTER.get(
    "/inputs",
    responses={
        200: {
            "description": "Successfully retrieved the input file",
            "content": {"application/octet-stream": {}},
        },
        404: {
            "description": "Task or input file not found",
            "content": {
                "application/json": {"example": {"detail": "Task(task_id): input file `file_name` was not found."}}
            },
        },
    },
)
async def get_task_inputs(
    request: Request,
    task_id: int = Query(..., description="ID of the task"),
    input_index: int = Query(..., description="Index of the input file"),
):
    """
    Retrieves a specific input file for a task, identified by `task_id` and `input_index`. This endpoint
    allows access to input files regardless of whether the task is in queue or has finished. The input index
    is used to select among multiple input files if more than one was provided for the task.
    Administrators can access inputs of any task, while regular users can only access inputs of their own tasks.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id)
    else:
        r = get_task(task_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if r["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    input_directory = os.path.join(options.TASKS_FILES_DIR, "input")
    for filename in os.listdir(input_directory):
        if filename == r["input_files"][input_index]["file_name"]:
            return responses.FileResponse(os.path.join(input_directory, filename))
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Task({r['task_id']}): input file `{r['input_files'][input_index]['file_name']}` was not found.",
    )


@ROUTER.get(
    "/results",
    responses={
        200: {
            "description": "Successfully retrieved the result file",
            "content": {"application/octet-stream": {}},
        },
        404: {
            "description": "Task or result file not found",
            "content": {
                "application/json": {"example": {"detail": "Missing result for task=task_id and node=node_id."}}
            },
        },
    },
)
async def get_task_results(
    request: Request,
    task_id: int = Query(..., description="ID of the task"),
    node_id: int = Query(..., description="ID of the node"),
    batch_index: int = Query(
        0,
        description="Optional index of the node result if the node produced more than one result. "
        "If set to -1, all results are returned as a ZIP archive.",
    ),
):
    """
    Retrieves the result file associated with a specific task and node ID. This function searches for
    output files in the designated output directory that match the task and node identifiers.

    Parameters:
    - task_id (int): ID of the task.
    - node_id (int): ID of the node.
    - batch_index (int, optional): Index of the node result if the node produced more than one result.
      - If set to 0 (default), the first result file is returned.
      - If set to a positive integer, the corresponding result file index is returned.
      - If set to -1, all results are returned as a ZIP archive.

    If the specific result file is not found, or if the task does not exist, a 404 HTTP error is returned.

    Returns:
    - FileResponse: The result file or a ZIP archive containing all result files if batch_index is -1.
    - HTTPException: If the task or result file is not found.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id, request.scope["user_info"].user_id)
    else:
        r = get_task(task_id, request.scope["user_info"].user_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    result_prefix = f"{task_id}_{node_id}_"
    output_files = get_task_files(task_id, "output")
    relevant_files = [file_info for file_info in output_files if file_info[0].startswith(result_prefix)]
    output_node = None
    for i in r["outputs"]:
        if i["comfy_node_id"] == node_id:
            output_node = i
            break
    if not output_node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No such node in the flow for task={task_id}."
        )
    if output_node["type"] == "image":
        relevant_files = [i for i in relevant_files if any(i[0].endswith(ext) for ext in etc.IMAGE_EXTENSIONS)]
    elif output_node["type"] == "video":
        relevant_files = [i for i in relevant_files if any(i[0].endswith(ext) for ext in etc.VIDEO_EXTENSIONS)]
    if not relevant_files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Missing result for task={task_id} and node={node_id}."
        )
    if batch_index == -1:
        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, "w") as zip_file:
            for file_info in relevant_files:
                file_name, file_path = file_info
                with builtins.open(file_path, "rb") as f:
                    zip_file.writestr(file_name, f.read())
        zip_buffer.seek(0)
        return responses.Response(
            content=zip_buffer.read(),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={task_id}_{node_id}_results.zip"},
        )
    if batch_index + 1 > len(relevant_files):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Missing result for task={task_id} and node={node_id}."
        )
    base_name, extension = os.path.splitext(relevant_files[batch_index][0])
    content_disposition = base_name[:-1] + extension if base_name.endswith("_") else base_name + extension
    return responses.FileResponse(relevant_files[batch_index][1], filename=content_disposition)


@ROUTER.delete(
    "/queue",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully cleared unfinished tasks from the queue"},
    },
)
async def remove_tasks_from_queue(
    request: Request,
    name: str = Query(..., description="Name of the task to clear unfinished tasks from the queue"),
    group_scope: int = Query(
        1, description="Optional group scope to filter tasks only belonging to a specific group. Defaults to 1."
    ),
):
    """
    Clears all unfinished tasks from the queue for a specific task name, scoped to the requesting user and group scope.
    Child tasks are ignored and not removed from the queue.
    """
    remove_unfinished_tasks_by_name_and_group(name, request.scope["user_info"].user_id, group_scope)


@ROUTER.delete(
    "/queue/{task_id}",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully removed the unfinished task from the queue"},
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def remove_task_from_queue(request: Request, task_id: int):
    """
    Removes a specific unfinished task from the queue using the task ID.
    """
    if get_task(task_id, request.scope["user_info"].user_id) is None:
        raise HTTPException(status_code=404, detail=f"Task `{task_id}` was not found.")
    remove_unfinished_task_by_id(task_id)


@ROUTER.post(
    "/next",
    responses={
        200: {
            "description": "Successfully retrieved the task for the worker",
        },
        204: {
            "description": "No incomplete tasks available for the worker",
        },
    },
)
async def get_next_task(
    request: Request,
    worker_details: WorkerDetailsRequest = Body(...),
    tasks_names: list[str] = Body(..., description="List of task names the worker can handle"),
    last_task_name: str = Body("", description="Optional name of the last task the worker was working on"),
):
    """
    Retrieves an incomplete task for a `worker` to process. Workers provide a list of tasks names they can handle
    and optionally the name of the last task they were working on to prioritize similar types of tasks. If a
    worker is associated with an admin account, it can retrieve tasks regardless of user assignment; otherwise,
    it retrieves only those assigned to the user.
    """
    user_id = None if request.scope["user_info"].is_admin else request.scope["user_info"].user_id
    if options.VIX_MODE == "SERVER":
        task = await get_incomplete_task_without_error_database_async(
            request.scope["user_info"].user_id, worker_details, tasks_names, last_task_name, user_id
        )
    else:
        task = get_incomplete_task_without_error_database(
            request.scope["user_info"].user_id, worker_details, tasks_names, last_task_name, user_id
        )
    if not task:
        return responses.Response(status_code=status.HTTP_204_NO_CONTENT)
    return task


async def __webhook_task_progress(
    url: str, headers: dict | None, task_id: int, progress: float, execution_time: float, error: str
) -> None:
    try:
        async with httpx.AsyncClient(base_url=url, timeout=3.0) as client:
            await client.post(
                url="task-progress",
                json={
                    "task_id": task_id,
                    "progress": progress,
                    "execution_time": execution_time,
                    "error": error,
                },
                headers=headers,
            )
    except httpx.RequestError as e:
        LOGGER.exception("Exception during calling webhook %s, progress=%s: %s", url, progress, e)


@ROUTER.put(
    "/progress",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {
            "description": "Task progress updated successfully",
        },
        400: {
            "description": "Failed to update task progress",
            "content": {"application/json": {"example": {"detail": "Failed to update task progress."}}},
        },
        404: {
            "description": "Task not found or not authorized",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def update_task_progress(
    b_tasks: BackgroundTasks,
    request: Request,
    worker_details: WorkerDetailsRequest = Body(...),
    task_id: int = Body(..., description="ID of the task to update progress for"),
    progress: float = Body(..., description="Progress percentage of the task"),
    execution_time: float = Body(..., description="Execution time of the task in seconds"),
    error: str = Body("", description="Error message if any"),
):
    """
    Updates the progress of a specific task identified by `task_id`. This endpoint checks if the task exists
    and if the requester is authorized to update its progress. If the task is not found or unauthorized,
    a 404 HTTP error is raised, and `worker` should stop and consider the task canceled.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id)
    else:
        r = get_task(task_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if r["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if options.VIX_MODE == "SERVER":
        update_success = await update_task_progress_database_async(
            task_id, progress, error, execution_time, request.scope["user_info"].user_id, worker_details
        )
    else:
        update_success = update_task_progress_database(
            task_id, progress, error, execution_time, request.scope["user_info"].user_id, worker_details
        )
    if not update_success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to update task progress.")
    if r["webhook_url"]:
        b_tasks.add_task(
            __webhook_task_progress, r["webhook_url"], r["webhook_headers"], task_id, progress, execution_time, error
        )


@ROUTER.put(
    "/results",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully saved task results"},
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
        400: {
            "description": "Bad request",
            "content": {"application/json": {"example": {"detail": "result_file.filename does not belong to task."}}},
        },
    },
)
async def set_task_results(
    request: Request,
    task_id: int = Query(..., description="The ID of the task to save results for"),
    files: list[UploadFile] = Form(..., description="List of result files to save"),  # noqa
):
    """
    Saves the result files for a specific task on the server. This endpoint checks if the task exists
    and if the `worker` making the request has the authorization to upload results.
    If the task is not found or unauthorized, a 404 HTTP error is raised.
    """
    if options.VIX_MODE == "SERVER":
        task_details = await get_task_async(task_id)
    else:
        task_details = get_task(task_id)
    if task_details is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if task_details["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    output_directory = os.path.join(options.TASKS_FILES_DIR, "output")
    for task_output in task_details["outputs"]:
        task_file_prefix = f"{task_id}_{task_output['comfy_node_id']}_"
        relevant_files = [file_info for file_info in files if file_info.filename.startswith(task_file_prefix)]
        if not relevant_files:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No results found for: {task_file_prefix}",
            )
        file_size = 0
        batch_size = 0
        for i in relevant_files:
            file_size += i.size
            batch_size += 1
            try:
                file_path = Path(output_directory).joinpath(i.filename)
                with builtins.open(file_path, mode="wb") as out_file:
                    shutil.copyfileobj(i.file, out_file)
            finally:
                i.file.close()
        task_output["file_size"] = file_size
        task_output["batch_size"] = batch_size
    if options.VIX_MODE == "SERVER":
        await update_task_outputs_async(task_id, task_details["outputs"])
    else:
        update_task_outputs(task_id, task_details["outputs"])


@ROUTER.delete(
    "/lock",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully removed task lock"},
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def remove_task_lock(
    request: Request, task_id: int = Query(..., description="The ID of the task to remove the lock from")
):
    """
    Unlocks a task specified by the `task_id`. This endpoint checks if the task exists
    and if the `worker` making the request has the authorization to unlock it.
    If the task is not found or unauthorized, a 404 HTTP error is raised.
    """
    if options.VIX_MODE == "SERVER":
        r = await get_task_async(task_id)
    else:
        r = get_task(task_id)
    if r is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    if r["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")
    remove_task_lock_database(task_id)


@ROUTER.put(
    "/update",
    response_class=responses.Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Successfully updated the task"},
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "examples": {
                        "Task started": {
                            "summary": "Task already started",
                            "value": {"detail": "Task `{task_id}` cannot be updated because it has already started."},
                        },
                        "Invalid priority": {
                            "summary": "Invalid priority",
                            "value": {"detail": "Priority cannot be greater than 15."},
                        },
                        "No fields": {
                            "summary": "No fields to update",
                            "value": {"detail": "No valid fields to update."},
                        },
                    }
                }
            },
        },
        404: {
            "description": "Task not found",
            "content": {"application/json": {"example": {"detail": "Task `{task_id}` was not found."}}},
        },
    },
)
async def update_task_info(
    request: Request,
    task_id: int = Query(..., description="ID of the task to update"),
    update_data: TaskUpdateRequest = Body(..., description="Fields to update"),
):
    """
    Updates the information of a task specified by `task_id`. Only tasks that have not yet started (progress == 0.0)
    can be updated. Currently, only the `priority` field can be updated.

    The `priority` parameter must not exceed 15.

    Access is restricted to the task owner or an administrator.
    """
    if options.VIX_MODE == "SERVER":
        task = await get_task_async(task_id)
    else:
        task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")

    if task["user_id"] != request.scope["user_info"].user_id and not request.scope["user_info"].is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task `{task_id}` was not found.")

    if task["progress"] != 0.0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task `{task_id}` cannot be updated because it has already started.",
        )

    update_fields = update_data.model_dump(exclude_unset=True)
    if not update_fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid fields to update.")

    if "priority" in update_fields and update_fields["priority"] > 15:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Priority cannot be greater than 15.")

    update_fields["priority"] = ((task["group_scope"] - 1) << 4) + update_fields["priority"]

    if options.VIX_MODE == "SERVER":
        success = await update_task_info_database_async(task_id, update_fields)
    else:
        success = update_task_info_database(task_id, update_fields)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to update task `{task_id}`.")
