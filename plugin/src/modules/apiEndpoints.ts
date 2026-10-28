/** Accept a pasted Chat Completions URL without doubling its path in the SDK. */
export function normalizeChatBaseUrl(service: string, value: string): string {
    const trimmed = String(value || "").trim();
    if (!["openai", "openailiked", "openaicompatible"].includes(service)) {
        return trimmed;
    }
    try {
        const url = new URL(trimmed);
        if (!["http:", "https:"].includes(url.protocol)) return trimmed;
        const path = url.pathname.replace(/\/+$/, "");
        if (!path.endsWith("/chat/completions")) return trimmed;
        url.pathname = path.slice(0, -"/chat/completions".length) || "/";
        return url.toString().replace(/\/$/, "");
    } catch {
        return trimmed;
    }
}
