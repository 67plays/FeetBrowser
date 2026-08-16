/*
 * FeetBrowser.app/Contents/MacOS/FeetBrowser -- the bundle's main executable.
 *
 * This is a real Mach-O that embeds the bundled CPython and runs
 * `-m feetbrowser` in it. The obvious alternative -- a shell script that
 * execs the interpreter -- is what macOS makes impossible to use here.
 * AppKit answers "which app am I?" with `[NSBundle mainBundle]`, which walks
 * up from the path of the currently executing image; after an exec that path
 * is the interpreter's, so the running app would be Python.framework's own
 * embedded Python.app and would take its name, its icon and its bundle
 * identifier from that plist instead of ours. doormat/cocoa.py calls
 * `setActivationPolicy:NSApplicationActivationPolicyRegular` and
 * `activateIgnoringOtherApps:`, so it gets a Dock tile and the keyboard
 * either way -- what it cannot get from a script is that the tile is
 * FeetBrowser's. Keeping the process image *inside* Contents/MacOS is the
 * whole reason this file exists.
 *
 * Everything the interpreter needs to find is computed from the executable's
 * own path, so the bundle works from /Applications, from a mounted disk
 * image, from a folder whose name has spaces in it, and from wherever a user
 * has dragged and renamed it.
 */
#include <Python.h>

#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Kept in step with the framework build.sh embeds. */
#define PYVER "3.13"
#define PYVER_NODOT "313"

static void die(const char *what) {
    fprintf(stderr, "FeetBrowser: %s\n", what);
    exit(1);
}

/* `base` + "/" + `tail`, into a fresh PATH_MAX buffer. */
static char *under(const char *base, const char *tail) {
    char *out = malloc(PATH_MAX);
    if (!out || snprintf(out, PATH_MAX, "%s/%s", base, tail) >= PATH_MAX)
        die("bundle path is too long");
    return out;
}

static void check(PyStatus status, PyConfig *config) {
    if (!PyStatus_Exception(status))
        return;
    PyConfig_Clear(config);
    if (PyStatus_IsExit(status))
        exit(status.exitcode);
    Py_ExitStatusException(status);   /* prints the reason, then exits */
}

static void add_path(PyConfig *config, const char *path) {
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (!wide)
        die("cannot decode a bundle path");
    check(PyWideStringList_Append(&config->module_search_paths, wide), config);
    PyMem_RawFree(wide);
}

int main(int argc, char **argv) {
    /* _NSGetExecutablePath can hand back a path with symlinks and ".." in
     * it; realpath is what makes "two directories up" mean Contents. */
    char raw[PATH_MAX];
    uint32_t size = sizeof(raw);
    if (_NSGetExecutablePath(raw, &size) != 0)
        die("cannot find my own executable");
    char self[PATH_MAX];
    if (!realpath(raw, self))
        memcpy(self, raw, sizeof(self));

    /* dirname() is allowed to write through its argument and to answer from
     * static storage, so it gets a private buffer each time rather than being
     * nested. Two levels up from Contents/MacOS/FeetBrowser is Contents. */
    char work[PATH_MAX];
    snprintf(work, sizeof(work), "%s", self);
    char macos[PATH_MAX];
    snprintf(macos, sizeof(macos), "%s", dirname(work));
    char *contents = strdup(dirname(macos));
    if (!contents)
        die("out of memory");

    char *home = under(contents, "Frameworks/Python.framework/Versions/" PYVER);
    char *applib = under(contents, "Resources/lib");
    char *certs = under(contents, "Resources/certs/cacert.pem");
    char *certdir = under(contents, "Resources/certs");

    /* A bundled CPython has no CA store: python.org's OpenSSL is compiled to
     * look under the framework's own etc/openssl, that directory ships empty
     * (which is what the installer's "Install Certificates.command" exists to
     * fix), and the compiled-in path would point at /Library in any case. So
     * the trust store travels in the bundle and is named here. `ssl` reads
     * these two variables every time it builds a default context, so setting
     * them before the interpreter starts is enough, and setenv's overwrite
     * flag is 0 so anyone who has already chosen a store keeps it. */
    setenv("SSL_CERT_FILE", certs, 0);
    setenv("SSL_CERT_DIR", certdir, 0);

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    /* argv is ours to hand over, not Python's to interpret: without this a
     * user's URL beginning with "-" would be read as an interpreter flag. */
    config.parse_argv = 0;
    /* The bundle may sit on a read-only disk image, and even when it does
     * not, an app must not scribble .pyc files into /Applications. build.sh
     * compiles everything ahead of time, so there is nothing to write. */
    config.write_bytecode = 0;
    /* Keep the working directory off sys.path. Finder launches with cwd "/",
     * and a browser that imports whatever happens to be next to it is a bug
     * waiting for someone to open the app from a downloads folder. */
    config.safe_path = 1;
    config.pathconfig_warnings = 0;

    check(PyConfig_SetBytesString(&config, &config.program_name, self), &config);
    check(PyConfig_SetBytesString(&config, &config.home, home), &config);

    /* sys.path in full, rather than letting getpath deduce it: the layout is
     * known exactly here and a deduced one is a silent fallback to whatever
     * Python happens to find on the machine. */
    config.module_search_paths_set = 1;
    char stdlib[PATH_MAX];
    snprintf(stdlib, sizeof(stdlib), "%s/lib/python" PYVER, home);
    char zip[PATH_MAX];
    snprintf(zip, sizeof(zip), "%s/lib/python" PYVER_NODOT ".zip", home);
    char dynload[PATH_MAX];
    snprintf(dynload, sizeof(dynload), "%s/lib-dynload", stdlib);
    add_path(&config, zip);
    add_path(&config, stdlib);
    add_path(&config, dynload);
    add_path(&config, applib);

    check(PyConfig_SetBytesArgv(&config, argc, argv), &config);
    check(PyConfig_SetBytesString(&config, &config.run_module, "feetbrowser"),
          &config);

    check(Py_InitializeFromConfig(&config), &config);
    PyConfig_Clear(&config);

    free(contents);
    free(home);
    free(applib);
    free(certs);
    free(certdir);
    return Py_RunMain();
}
