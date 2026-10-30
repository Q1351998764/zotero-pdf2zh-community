import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

// Run the actual page's rendering functions with a minimal DOM sink. This
// checks file availability and escaped warnings, without booting a server.
const html = readFileSync(
    new URL("../../server/index.html", import.meta.url),
    "utf8",
);
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);
const syntax = ts.createSourceFile(
    "progress.js",
    script,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JS,
);
const selected = new Set([
    "escapeHtml",
    "taskWarning",
    "renderTranslationReport",
    "rememberReportOpen",
    "renderConfigHtml",
    "normalizeTaskState",
    "sortTasks",
    "filterTasks",
    "updateTasks",
    "displayHistory",
]);
const functions = syntax.statements
    .filter(
        (statement) =>
            ts.isFunctionDeclaration(statement) &&
            selected.has(statement.name?.text || ""),
    )
    .map((statement) => statement.getText(syntax))
    .join("\n");

function renderer() {
    const elements = new Map<
        string,
        {
            innerHTML: string;
            textContent: string;
            className: string;
            style: Record<string, string>;
        }
    >();
    const document = {
        getElementById: (id: string) => {
            if (!elements.has(id))
                elements.set(id, {
                    innerHTML: "",
                    textContent: "",
                    className: "",
                    style: {},
                });
            return elements.get(id)!;
        },
    };
    const context = vm.createContext({
        document,
        currentTaskFilter: "all",
        expandedConfigTasks: new Set(),
        expandedReportTasks: new Set(),
        notifiedTaskIds: new Set(),
        lastTasksList: [],
        activeTasks: {},
        updateTaskTimer() {},
        stopTaskTimer() {},
        playDing() {},
        syncHistory() {},
        setTimeout() {},
        formatTime: () => "12:00",
        formatDuration: () => "1分",
        calculateDuration: () => "1分",
        getFileTypeLabel: () => ({ code: "mono", desc: "译文" }),
    });
    vm.runInContext(functions, context);
    return { context, elements };
}

test("warning completion stays successful and preserves PDF download and preview", () => {
    const { context, elements } = renderer();
    const task = {
        taskId: "one",
        status: "success",
        finished: true,
        active: false,
        fileName: "paper.pdf",
        fileList: ["author's.pdf"],
        warning: "有 1 个段落未译：<img src=x onerror=alert(1)> & 连接中断",
    };
    assert.equal(context.normalizeTaskState(task).state, "success");
    assert.equal(
        context.normalizeTaskState(task).badgeText,
        "完成（有未译段落）",
    );
    context.updateTasks([task]);
    const activeMarkup = elements.get("tasksContainer")!.innerHTML;
    assert.match(activeMarkup, /task-card warning/);
    assert.match(activeMarkup, /&lt;img/);
    assert.doesNotMatch(activeMarkup, /<img/);
    context.displayHistory([task]);
    const historyMarkup = elements.get("historyList")!.innerHTML;
    assert.match(historyMarkup, /完成（有未译段落）/);
    assert.match(historyMarkup, /class="file-btn download"/);
    assert.match(historyMarkup, /class="file-btn preview"/);
    assert.match(historyMarkup, /data-filename="author&#39;s.pdf"/);
    assert.match(historyMarkup, /&lt;img/);
    assert.doesNotMatch(historyMarkup, /<img|error-message/);
});

test("ordinary success has no warning and real failure has no success download controls", () => {
    const { context, elements } = renderer();
    const success = {
        taskId: "one",
        active: false,
        status: "success",
        fileList: ["done.pdf"],
    };
    assert.equal(context.normalizeTaskState(success).badgeText, "成功");
    context.displayHistory([success]);
    assert.doesNotMatch(
        elements.get("historyList")!.innerHTML,
        /warning-message/,
    );
    context.displayHistory([
        { ...success, status: "failed", error: "<broken>" },
    ]);
    const markup = elements.get("historyList")!.innerHTML;
    assert.match(markup, /&lt;broken&gt;/);
    assert.doesNotMatch(markup, /file-btn download/);
});

test("completion reports show each cause count, log detail and advice as safe text", () => {
    const { context, elements } = renderer();
    const task = {
        taskId: "report",
        status: "success",
        active: false,
        finished: true,
        fileList: ["translated.pdf"],
        warning: "有 3 个段落未译，PDF 仍可下载。",
        translationReport: {
            failedParagraphs: 3,
            reasons: [
                {
                    code: "connection",
                    label: "连接中断",
                    count: 2,
                    detail: "RemoteProtocolError: <svg onload=alert(1)>",
                    advice: "检查网络；不能仅凭此认定并发过高。",
                },
                {
                    code: "rate_limit",
                    label: "接口限流",
                    count: 1,
                    detail: "HTTP 429",
                    advice: "降低 QPS & 检查配额 <script>",
                },
            ],
        },
    };
    context.updateTasks([task]);
    context.displayHistory([task]);
    for (const id of ["tasksContainer", "historyList"]) {
        const markup = elements.get(id)!.innerHTML;
        assert.match(markup, /连接中断：2 段/);
        assert.match(markup, /接口限流：1 段/);
        assert.match(markup, /日志：RemoteProtocolError: &lt;svg/);
        assert.match(markup, /建议：检查网络；不能仅凭此认定并发过高。/);
        assert.match(markup, /降低 QPS &amp; 检查配额 &lt;script&gt;/);
        assert.doesNotMatch(markup, /<svg onload|<script>/);
    }
    assert.match(
        elements.get("historyList")!.innerHTML,
        /class="file-btn download"/,
    );
    assert.equal(context.normalizeTaskState(task).state, "success");
});

test("report open and closed choices survive repeated SSE renders and history refresh", () => {
    const { context, elements } = renderer();
    const task = {
        taskId: "report-1",
        status: "success",
        active: false,
        finished: true,
        warning: "有 1 个段落未译",
        translationReport: {
            failedParagraphs: 1,
            reasons: [{ label: "连接中断", count: 1 }],
        },
    };
    const isOpen = /<details[^>]*\sopen(?:\s|>)/;
    context.updateTasks([task]);
    assert.doesNotMatch(elements.get("tasksContainer")!.innerHTML, isOpen);
    // The page's own toggle handler records the user action before SSE replaces
    // the element. A detached old element cannot overwrite that remembered state.
    context.rememberReportOpen({
        isConnected: true,
        open: true,
        dataset: { reportTaskId: "report-1" },
    });
    context.rememberReportOpen({
        isConnected: false,
        open: false,
        dataset: { reportTaskId: "report-1" },
    });
    for (let tick = 0; tick < 3; tick++) {
        context.updateTasks([task]);
        context.displayHistory([task]);
        for (const id of ["tasksContainer", "historyList"]) {
            assert.match(elements.get(id)!.innerHTML, isOpen);
        }
    }
    assert.doesNotMatch(
        context.renderTranslationReport({ ...task, taskId: "other" }),
        isOpen,
    );
    context.rememberReportOpen({
        isConnected: true,
        open: false,
        dataset: { reportTaskId: "report-1" },
    });
    context.updateTasks([task]);
    context.displayHistory([task]);
    for (const id of ["tasksContainer", "historyList"]) {
        assert.doesNotMatch(elements.get(id)!.innerHTML, isOpen);
    }
});
