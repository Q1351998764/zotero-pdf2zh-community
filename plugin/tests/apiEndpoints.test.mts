import assert from "node:assert/strict";
import test from "node:test";
import { normalizeChatBaseUrl } from "../src/modules/apiEndpoints.ts";

test("pasted NVIDIA chat endpoint becomes a valid base URL", () => {
    assert.equal(
        normalizeChatBaseUrl(
            "openailiked",
            " https://integrate.api.nvidia.com/v1/chat/completions/ ",
        ),
        "https://integrate.api.nvidia.com/v1",
    );
});

test("custom base paths and other provider protocols are preserved", () => {
    assert.equal(
        normalizeChatBaseUrl(
            "openai",
            "https://example.org/proxy/v2/chat/completions",
        ),
        "https://example.org/proxy/v2",
    );
    assert.equal(
        normalizeChatBaseUrl("openai", "https://example.org/v1"),
        "https://example.org/v1",
    );
    assert.equal(
        normalizeChatBaseUrl("gemini", "https://example.org/chat/completions"),
        "https://example.org/chat/completions",
    );
});
