import json
import os
import threading
import time
from datetime import datetime
from utils.diagnostics import safe_message


# DEBUG_PROGRESS_LOG_PATH = os.path.join(
#     os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
#     "_debug_progress.log",
# )
# _DEBUG_LOG_LOCK = threading.Lock()


# def _debug_progress_log(stage, **fields):
#     try:
#         ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#         parts = []
#         for key in sorted(fields.keys()):
#             value = str(fields[key]).replace("\r", "\\r").replace("\n", "\\n")
#             if len(value) > 800:
#                 value = value[:800] + "..."
#             parts.append(f"{key}={value}")
#         line = f"[{ts}] [{stage}] " + " ".join(parts)
#         with _DEBUG_LOG_LOCK:
#             with open(DEBUG_PROGRESS_LOG_PATH, "a", encoding="utf-8", errors="replace") as fp:
#                 fp.write(line + "\n")
#     except Exception:
#         pass


class TaskManager:
    def __init__(self):
        self.active_tasks = {}
        self.lock = threading.Lock()
        self.progress_history = []

    @staticmethod
    def _task_snapshot(task_id, task):
        snapshot = {
            "taskId": task_id,
            "fileName": task.get("fileName"),
            "active": task.get("active"),
            "progress": task.get("progress"),
            "status": task.get("status"),
            "message": task.get("message"),
            "engine": task.get("engine"),
            "service": task.get("service"),
        }
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    def add_task(self, task_id, info):
        with self.lock:
            self.active_tasks[task_id] = info
            # _debug_progress_log("TASK_ADD", task=self._task_snapshot(task_id, self.active_tasks[task_id]))

    def update_task(self, task_id, updates):
        updates = dict(updates)
        for key in ("message", "error", "diagnosticSummary", "diagnosticTail", "warning"):
            if key in updates:
                updates[key] = safe_message(updates[key])
        with self.lock:
            if task_id in self.active_tasks:
                self.active_tasks[task_id].update(updates)
                # _debug_progress_log(
                #     "TASK_UPDATE",
                #     updates=json.dumps(updates, ensure_ascii=False, sort_keys=True),
                #     task=self._task_snapshot(task_id, self.active_tasks[task_id]),
                # )

    def get_task(self, task_id):
        with self.lock:
            task = self.active_tasks.get(task_id)
            return dict(task) if task else None

    def complete_task(self, task_id, status, message=None, file_list=None, error=None, file_paths=None, output_dir=None, result=None):
        message = safe_message(message) if message else None
        error = safe_message(error) if error else None
        with self.lock:
            if task_id not in self.active_tasks:
                return
            task = self.active_tasks[task_id]
            # Client disconnect after a successful translate can raise while
            # returning HTTP; do not overwrite a completed success as failure.
            if task.get("active") is False and task.get("finished"):
                return
            task["active"] = False
            task["finished"] = True
            task["status"] = "完成" if status == "success" else "失败"
            task["progress"] = 100 if status == "success" else task.get("progress", 0)
            if message:
                task["message"] = message
            task["endTime"] = datetime.now().isoformat()
            if file_list:
                task["fileList"] = list(file_list)
            if file_paths:
                task["filePaths"] = list(file_paths)
            if output_dir:
                task["outputDir"] = output_dir
            if result:
                task["result"] = result
            if error:
                task["error"] = str(error)

            history_item = {
                "taskId": task_id,
                "fileName": task.get("fileName"),
                "status": "success" if status == "success" else "failed",
                "finished": True,
                "active": False,
                "engine": task.get("engine"),
                "service": task.get("service"),
                "startTime": task.get("startTime"),
                "endTime": task.get("endTime"),
                "config": task.get("config"),
                "message": task.get("message"),
                "modelName": task.get("modelName"),
            }
            for key in ("logFile", "logPath", "diagnosticSummary", "warning", "translationReport"):
                if task.get(key):
                    history_item[key] = task[key]
            if file_list:
                history_item["fileList"] = list(file_list)
            if file_paths:
                history_item["filePaths"] = list(file_paths)
            if output_dir:
                history_item["outputDir"] = output_dir
            if result:
                history_item["result"] = result
            if error:
                history_item["error"] = str(error)

            self.progress_history.insert(0, history_item)
            if len(self.progress_history) > 200:
                self.progress_history = self.progress_history[:200]

            threading.Thread(target=self._delayed_remove, args=(task_id,), daemon=True).start()

    def get_active_tasks_list(self):
        with self.lock:
            # Full diagnostic tails stay in the per-task log, not in every SSE.
            tasks = [{key: value for key, value in task.items() if key != "diagnosticTail"}
                     for task in self.active_tasks.values()]
            return tasks

    def get_history(self):
        with self.lock:
            return list(self.progress_history)

    def _delayed_remove(self, task_id):
        time.sleep(30)
        with self.lock:
            if task_id in self.active_tasks:
                # _debug_progress_log("TASK_REMOVE", task_id=task_id)
                del self.active_tasks[task_id]


# global singleton
task_manager = TaskManager()
