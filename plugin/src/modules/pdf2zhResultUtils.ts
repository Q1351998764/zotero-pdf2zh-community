type ServerPayload = {
    status: string;
    message?: string;
    [key: string]: unknown;
};

function object(value: unknown): Record<string, unknown> | undefined {
    return value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : undefined;
}

function stringList(value: unknown): string[] {
    return Array.isArray(value)
        ? value.filter(
              (item): item is string => typeof item === "string" && !!item,
          )
        : [];
}

export function translationWarning(payload: Record<string, unknown>): string {
    const nested = object(payload.result);
    const warning = [payload.warning, nested?.warning].find(
        (value) => typeof value === "string" && value.trim(),
    );
    if (typeof warning === "string") {
        return warning.trim().slice(0, 2000);
    }
    const report = object(
        payload.translationReport ?? nested?.translationReport,
    );
    const count = report?.failedParagraphs;
    if (typeof count !== "number" || !Number.isInteger(count) || count <= 0) {
        return "";
    }
    const reasons = Array.isArray(report?.reasons)
        ? report.reasons.flatMap((value) => {
              const reason = object(value);
              const label = reason?.label;
              return typeof label === "string" && label.trim()
                  ? [label.trim()]
                  : [];
          })
        : [];
    return `有 ${count} 个段落未译，已保留原文。${reasons.length ? `原因：${reasons.join("；")}。` : "详见后端任务日志。"}`;
}

export function completedTaskPayload(
    task: Record<string, unknown> | undefined,
    taskId: string,
): ServerPayload | null {
    if (!task || Array.isArray(task.tasks) || Array.isArray(task.history)) {
        return null;
    }
    const rawId = task.taskId ?? task.task_id;
    const id =
        typeof rawId === "string" || typeof rawId === "number"
            ? String(rawId).trim()
            : "";
    if (!id || id !== taskId) return null;
    const nested = object(task.result);
    const status = String(task.status || nested?.status || "");
    const failed =
        status === "失败" ||
        status === "failed" ||
        nested?.status === "error" ||
        (task.finished === true && status !== "完成" && status !== "success");
    if (failed) {
        return {
            status: "error",
            message: String(
                nested?.message || task.error || task.message || "翻译失败",
            ),
        };
    }
    const files = stringList(nested?.fileList ?? task.fileList);
    const paths = stringList(nested?.filePaths ?? task.filePaths);
    const succeeded =
        (task.finished === true ||
            status === "完成" ||
            status === "success" ||
            nested?.status === "success") &&
        (files.length > 0 || paths.length > 0);
    if (!succeeded) return null;
    // Older Server histories may keep metadata on the task rather than result.
    // A warning changes the completion message, never file delivery status.
    const result: ServerPayload = {
        ...(nested?.status === "success" ? nested : {}),
        status: "success",
        fileList: files,
        filePaths: paths,
        outputDir: String(nested?.outputDir || task.outputDir || ""),
    };
    const warning = translationWarning(task);
    const report = task.translationReport ?? nested?.translationReport;
    if (warning) result.warning = warning;
    if (report) result.translationReport = report;
    return result;
}
