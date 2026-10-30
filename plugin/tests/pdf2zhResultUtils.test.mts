import assert from "node:assert/strict";
import test from "node:test";
import {
    completedTaskPayload,
    translationWarning,
} from "../src/modules/pdf2zhResultUtils.ts";

const report = {
    failedParagraphs: 2,
    reasons: [{ code: "connection", label: "连接中断", count: 2 }],
};
const warning = "有 2 个段落未译，原因：连接中断；不能仅据此确定是否并发过高。";

test("completed asynchronous task keeps PDF delivery and the warning report", () => {
    const payload = completedTaskPayload(
        {
            taskId: "one",
            finished: true,
            status: "完成",
            result: {
                status: "success",
                fileList: ["paper.mono.pdf"],
                filePaths: ["D:/translated/paper.mono.pdf"],
                outputDir: "D:/translated",
                warning,
                translationReport: report,
            },
        },
        "one",
    );
    assert.equal(payload?.status, "success");
    assert.deepEqual(payload?.fileList, ["paper.mono.pdf"]);
    assert.deepEqual(payload?.filePaths, ["D:/translated/paper.mono.pdf"]);
    assert.equal(payload?.warning, warning);
    assert.deepEqual(payload?.translationReport, report);
    assert.equal(translationWarning(payload!), warning);
});

test("history-level warning survives a nested result containing only files", () => {
    const payload = completedTaskPayload(
        {
            taskId: "one",
            finished: true,
            status: "success",
            warning,
            translationReport: report,
            result: { status: "success", fileList: ["done.pdf"] },
        },
        "one",
    );
    assert.equal(payload?.status, "success");
    assert.equal(payload?.warning, warning);
    assert.deepEqual(payload?.translationReport, report);
});

test("ordinary success remains unqualified and a report can supply a missing warning", () => {
    const payload = completedTaskPayload(
        {
            taskId: "one",
            finished: true,
            status: "success",
            fileList: ["done.pdf"],
        },
        "one",
    );
    assert.equal(payload?.status, "success");
    assert.equal(translationWarning(payload!), "");
    assert.equal("warning" in payload!, false);
    assert.match(
        translationWarning({ translationReport: report }),
        /2 个段落未译.*连接中断/,
    );
    assert.equal(
        translationWarning({ warning: "", result: { warning } }),
        warning,
    );
    assert.equal(
        translationWarning({ translationReport: { failedParagraphs: "2" } }),
        "",
    );
});

test("warnings do not hide an actual failed job or manufacture missing files", () => {
    const failed = completedTaskPayload(
        {
            taskId: "one",
            finished: true,
            status: "failed",
            warning,
            error: "引擎退出失败",
            fileList: ["stale.pdf"],
        },
        "one",
    );
    assert.equal(failed?.status, "error");
    assert.equal(failed?.message, "引擎退出失败");
    assert.equal(
        completedTaskPayload(
            { taskId: "one", finished: true, status: "完成", warning },
            "one",
        ),
        null,
    );
    assert.equal(
        completedTaskPayload(
            {
                taskId: "another",
                finished: true,
                status: "完成",
                fileList: ["done.pdf"],
            },
            "one",
        ),
        null,
    );
});
