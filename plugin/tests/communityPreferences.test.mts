import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

test("community migration copies only user values once, preserves both profiles", () => {
    const oldValues = new Map<string, string | number | boolean>([
        ["llmApis", '[{"apiKey":"test-only-secret"}]'],
        ["qps", 6],
        ["ocr", true],
    ]);
    const newValues = new Map<string, string | number | boolean>([["qps", 2]]);
    function branch(values: Map<string, string | number | boolean>) {
        return {
            getChildList: () => [...values.keys(), "defaultOnly"],
            prefHasUserValue: (key: string) => values.has(key),
            getPrefType: (key: string) =>
                ({ string: 32, number: 64, boolean: 128 })[
                    typeof values.get(key)
                ],
            getStringPref: (key: string) => values.get(key),
            getIntPref: (key: string) => values.get(key),
            getBoolPref: (key: string, fallback: boolean) =>
                values.get(key) ?? fallback,
            setStringPref: (key: string, value: string) =>
                values.set(key, value),
            setIntPref: (key: string, value: number) => values.set(key, value),
            setBoolPref: (key: string, value: boolean) =>
                values.set(key, value),
        };
    }
    const sandbox = vm.createContext({
        Services: {
            prefs: {
                getBranch: (prefix: string) =>
                    branch(
                        prefix === "extensions.zotero.pdf2zh."
                            ? oldValues
                            : newValues,
                    ),
            },
        },
    });
    const source = fs.readFileSync(
        new URL("../addon/bootstrap.js", import.meta.url),
        "utf8",
    );
    vm.runInContext(source, sandbox);
    vm.runInContext("migrateCommunityPreferences()", sandbox);
    assert.equal(newValues.get("llmApis"), oldValues.get("llmApis"));
    assert.equal(newValues.get("qps"), 2);
    assert.equal(newValues.get("ocr"), true);
    assert.equal(newValues.has("defaultOnly"), false);
    assert.equal(oldValues.get("qps"), 6);
    oldValues.set("ocr", false);
    vm.runInContext("migrateCommunityPreferences()", sandbox);
    assert.equal(newValues.get("ocr"), true);
});
