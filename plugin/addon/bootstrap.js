/* eslint-disable no-undef */

/**
 * Most of this code is from Zotero team's official Make It Red example[1]
 * or the Zotero 7 documentation[2].
 * [1] https://github.com/zotero/make-it-red
 * [2] https://www.zotero.org/support/dev/zotero_7_for_developers
 */

var chromeHandle;

function install(data, reason) {}

// Community fork, 2026-10-28: copy explicitly saved upstream preferences once.
// Values (including API keys) stay in the local Zotero profile and are never logged.
function migrateCommunityPreferences() {
    const source = Services.prefs.getBranch("extensions.zotero.pdf2zh.");
    const target = Services.prefs.getBranch("__prefsPrefix__.");
    if (target.getBoolPref("communityMigrated", false)) return;
    for (const key of source.getChildList("")) {
        if (!source.prefHasUserValue(key) || target.prefHasUserValue(key))
            continue;
        switch (source.getPrefType(key)) {
            case 32:
                target.setStringPref(key, source.getStringPref(key));
                break;
            case 64:
                target.setIntPref(key, source.getIntPref(key));
                break;
            case 128:
                target.setBoolPref(key, source.getBoolPref(key));
                break;
        }
    }
    target.setBoolPref("communityMigrated", true);
}

async function startup({ id, version, resourceURI, rootURI }, reason) {
    await Zotero.initializationPromise;
    migrateCommunityPreferences();

    // String 'rootURI' introduced in Zotero 7
    if (!rootURI) {
        rootURI = resourceURI.spec;
    }

    var aomStartup = Components.classes[
        "@mozilla.org/addons/addon-manager-startup;1"
    ].getService(Components.interfaces.amIAddonManagerStartup);
    var manifestURI = Services.io.newURI(rootURI + "manifest.json");
    chromeHandle = aomStartup.registerChrome(manifestURI, [
        ["content", "__addonRef__", rootURI + "content/"],
    ]);

    /**
     * Global variables for plugin code.
     * The `_globalThis` is the global root variable of the plugin sandbox environment
     * and all child variables assigned to it is globally accessible.
     * See `src/index.ts` for details.
     */
    const ctx = {
        rootURI,
    };
    ctx._globalThis = ctx;

    Services.scriptloader.loadSubScript(
        `${rootURI}/content/scripts/__addonRef__.js`,
        ctx,
    );
    Zotero.__addonInstance__.hooks.onStartup();
}

async function onMainWindowLoad({ window }, reason) {
    Zotero.__addonInstance__?.hooks.onMainWindowLoad(window);
}

async function onMainWindowUnload({ window }, reason) {
    Zotero.__addonInstance__?.hooks.onMainWindowUnload(window);
}

function shutdown({ id, version, resourceURI, rootURI }, reason) {
    if (reason === APP_SHUTDOWN) {
        return;
    }

    if (typeof Zotero === "undefined") {
        Zotero = Components.classes["@zotero.org/Zotero;1"].getService(
            Components.interfaces.nsISupports,
        ).wrappedJSObject;
    }
    Zotero.__addonInstance__?.hooks.onShutdown();

    Cc["@mozilla.org/intl/stringbundle;1"]
        .getService(Components.interfaces.nsIStringBundleService)
        .flushBundles();

    Cu.unload(`${rootURI}/content/scripts/__addonRef__.js`);

    if (chromeHandle) {
        chromeHandle.destruct();
        chromeHandle = null;
    }
}

function uninstall(data, reason) {}
