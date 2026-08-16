//! FeetBrowser.exe: the Windows front door.
//!
//! Windows cannot double-click a `.py`, so something has to be a real PE
//! binary. That is the entire job of this file: find the CPython DLL sitting
//! next to it, hand it `-m feetbrowser` plus whatever the user typed, and
//! return whatever exit code comes back.
//!
//! It loads `python3XX.dll` and calls `Py_Main` rather than spawning
//! `python.exe`, which buys three things a child process cannot:
//!
//!   * One process. One PID in Task Manager, one thing to kill, and a window
//!     whose taskbar entry belongs to FeetBrowser.exe and carries its icon.
//!   * No console. `python.exe` in the embeddable package is a console-
//!     subsystem binary, so launching it from Explorer flashes a black box
//!     unless it is smothered with CREATE_NO_WINDOW. This binary is built
//!     `windows_subsystem = "windows"` and there is no second process to
//!     smother.
//!   * The right DPI behaviour. `python.exe` ships a manifest that marks the
//!     process DPI-aware before any of our code runs, which makes
//!     `doormat/win32.py`'s own `SetProcessDpiAwarenessContext` call fail
//!     and leaves the browser on the coarser system-DPI path. Our manifest
//!     stays quiet about DPI so the backend can pick per-monitor-v2 itself.
//!
//! The DLL is resolved at run time with `LoadLibraryEx` + `GetProcAddress`,
//! not linked against. The embeddable package ships no import library and no
//! headers, and this way a Python version bump in the bundle needs no
//! recompile of this binary; it scans for whatever `python3NN.dll` is there.

// No console window, ever. This is the difference between double-clicking the
// icon and getting a browser, and double-clicking it and getting a black box
// with a browser somewhere behind it.
#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(not(windows))]
fn main() {
    eprintln!("The FeetBrowser launcher only builds and runs on Windows.");
    eprintln!("Everywhere else, run the browser with: python3 -m feetbrowser");
    std::process::exit(2);
}

#[cfg(windows)]
fn main() {
    win::run()
}

#[cfg(windows)]
mod win {
    use std::ffi::OsStr;
    use std::os::raw::{c_int, c_void};
    use std::os::windows::ffi::OsStrExt;
    use std::path::{Path, PathBuf};

    type Handle = *mut c_void;
    type Module = *mut c_void;

    const STD_INPUT_HANDLE: u32 = -10i32 as u32;
    const STD_OUTPUT_HANDLE: u32 = -11i32 as u32;
    const STD_ERROR_HANDLE: u32 = -12i32 as u32;
    const ATTACH_PARENT_PROCESS: u32 = -1i32 as u32;
    const LOAD_WITH_ALTERED_SEARCH_PATH: u32 = 0x0000_0008;
    const INVALID_HANDLE_VALUE: Handle = -1isize as Handle;
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const FILE_SHARE_READ: u32 = 0x0000_0001;
    const FILE_SHARE_WRITE: u32 = 0x0000_0002;
    const OPEN_EXISTING: u32 = 3;
    const MB_ICONERROR: u32 = 0x0000_0010;

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleFileNameW(module: Module, buf: *mut u16, size: u32) -> u32;
        fn LoadLibraryExW(name: *const u16, file: Handle, flags: u32) -> Module;
        fn GetProcAddress(module: Module, name: *const u8) -> *const c_void;
        fn SetDllDirectoryW(path: *const u16) -> i32;
        fn GetStdHandle(which: u32) -> Handle;
        fn SetStdHandle(which: u32, handle: Handle) -> i32;
        fn AttachConsole(pid: u32) -> i32;
        fn CreateFileW(
            name: *const u16,
            access: u32,
            share: u32,
            security: *mut c_void,
            disposition: u32,
            flags: u32,
            template: Handle,
        ) -> Handle;
        fn GetLastError() -> u32;
    }

    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(owner: Handle, text: *const u16, caption: *const u16, style: u32) -> i32;
    }

    /// `int Py_Main(int argc, wchar_t **argv)`.
    ///
    /// Part of CPython's stable ABI and exported by every `python3NN.dll`,
    /// which is what lets one build of this launcher drive whichever
    /// interpreter the bundle was assembled around.
    type PyMain = unsafe extern "C" fn(c_int, *mut *mut u16) -> c_int;

    fn wide(s: &OsStr) -> Vec<u16> {
        s.encode_wide().chain(std::iter::once(0)).collect()
    }

    fn wide_str(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// Say something and stop. A GUI-subsystem process that writes to a
    /// stderr nobody is reading and then exits non-zero looks, from Explorer,
    /// exactly like nothing happening at all -- so this puts it on the screen
    /// as well as down the pipe.
    fn die(message: &str) -> ! {
        eprintln!("FeetBrowser: {}", message);
        let text = wide_str(message);
        let caption = wide_str("FeetBrowser");
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                text.as_ptr(),
                caption.as_ptr(),
                MB_ICONERROR,
            );
        }
        std::process::exit(1)
    }

    /// The full path of this executable.
    ///
    /// Not `std::env::current_exe`, which resolves symlinks and can hand back
    /// a path in a different directory than the one the user launched; the
    /// whole bundle is found relative to this, so it has to be the literal
    /// file that is running.
    fn exe_path() -> PathBuf {
        let mut buf = vec![0u16; 512];
        loop {
            let n = unsafe { GetModuleFileNameW(std::ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) };
            if n == 0 {
                die("could not work out where FeetBrowser.exe is on disk.");
            }
            // Truncation is reported by filling the buffer exactly; the only
            // way to tell it apart from an exact fit is to grow and retry.
            if (n as usize) < buf.len() {
                buf.truncate(n as usize);
                return PathBuf::from(String::from_utf16_lossy(&buf));
            }
            buf = vec![0u16; buf.len() * 2];
        }
    }

    /// The `python3NN.dll` next to the launcher.
    ///
    /// Found by scanning rather than by a name baked in at compile time, so
    /// that bumping the interpreter in the bundle is a change to one build
    /// script and not a rebuild of this binary. `python3.dll` -- the stable
    /// ABI forwarder, which is also in the embeddable package -- is skipped:
    /// it re-exports a subset, and we want the real one.
    fn find_python_dll(dir: &Path) -> Option<PathBuf> {
        let mut best: Option<(u32, PathBuf)> = None;
        for entry in std::fs::read_dir(dir).ok()? {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            let name = entry.file_name().to_string_lossy().to_lowercase();
            let digits = match name
                .strip_prefix("python3")
                .and_then(|rest| rest.strip_suffix(".dll"))
            {
                Some(d) if !d.is_empty() && d.chars().all(|c| c.is_ascii_digit()) => d,
                _ => continue,
            };
            // "3" + the minor: python313.dll sorts above python39.dll, and a
            // bundle only ever has one anyway.
            let version: u32 = format!("3{}", digits).parse().unwrap_or(0);
            if best.as_ref().map_or(true, |(v, _)| version > *v) {
                best = Some((version, entry.path()));
            }
        }
        best.map(|(_, p)| p)
    }

    /// Reattach stdout/stderr to the console that launched us, if there is one.
    ///
    /// A `windows` subsystem binary gets no console, which is the point --
    /// double-clicking must not flash a black box. But `FeetBrowser.exe
    /// --version` typed at a prompt still has to print somewhere, and
    /// `AttachConsole(ATTACH_PARENT_PROCESS)` borrows the caller's.
    ///
    /// Only when there is nothing on the handles already: cmd.exe passes its
    /// redirections down to GUI processes just like console ones, so
    /// `FeetBrowser.exe --version > out.txt` already has a real file handle
    /// and stealing it back for the console would throw the output away.
    fn attach_parent_console() {
        let existing = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
        if !existing.is_null() && existing != INVALID_HANDLE_VALUE {
            return;
        }
        if unsafe { AttachConsole(ATTACH_PARENT_PROCESS) } == 0 {
            return; // Launched from Explorer. There is no console to attach to.
        }
        let conout = wide_str("CONOUT$");
        let conin = wide_str("CONIN$");
        unsafe {
            let out = CreateFileW(
                conout.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null_mut(),
                OPEN_EXISTING,
                0,
                std::ptr::null_mut(),
            );
            if out != INVALID_HANDLE_VALUE {
                SetStdHandle(STD_OUTPUT_HANDLE, out);
                SetStdHandle(STD_ERROR_HANDLE, out);
            }
            let inp = CreateFileW(
                conin.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null_mut(),
                OPEN_EXISTING,
                0,
                std::ptr::null_mut(),
            );
            if inp != INVALID_HANDLE_VALUE {
                SetStdHandle(STD_INPUT_HANDLE, inp);
            }
        }
    }

    pub fn run() {
        attach_parent_console();

        let exe = exe_path();
        let dir = exe
            .parent()
            .unwrap_or_else(|| die("FeetBrowser.exe is not in a directory."))
            .to_path_buf();

        let dll = find_python_dll(&dir).unwrap_or_else(|| {
            die(
                "this copy of FeetBrowser is incomplete: no python3NN.dll next to \
                 FeetBrowser.exe.\n\nUnzip the whole FeetBrowser folder and run it \
                 from there -- the .exe on its own is not the program.",
            )
        });

        // Take the current working directory out of the DLL search order and
        // put the bundle in its place. Everything the interpreter and its
        // extension modules need -- vcruntime140.dll, libssl-3.dll,
        // libffi-8.dll -- lives beside the .exe, and the .exe's own directory
        // is searched first regardless; this is about what must *not* be
        // found, which is a stray DLL in whatever folder the user happened to
        // be in.
        let dir_w = wide(dir.as_os_str());
        unsafe { SetDllDirectoryW(dir_w.as_ptr()) };

        let dll_w = wide(dll.as_os_str());
        let module = unsafe { LoadLibraryExW(dll_w.as_ptr(), std::ptr::null_mut(), LOAD_WITH_ALTERED_SEARCH_PATH) };
        if module.is_null() {
            die(&format!(
                "could not load {} (Windows error {}).\n\nThe usual cause is an \
                 incomplete copy of the folder.",
                dll.display(),
                unsafe { GetLastError() }
            ));
        }

        let entry = unsafe { GetProcAddress(module, b"Py_Main\0".as_ptr()) };
        if entry.is_null() {
            die(&format!(
                "{} is not a CPython runtime: it exports no Py_Main.",
                dll.display()
            ));
        }
        let py_main: PyMain = unsafe { std::mem::transmute(entry) };

        // What the interpreter is told to do. `-X utf8` because a page title
        // printed to a redirected stdout must not die on the console
        // codepage; `-m feetbrowser` because that module is the browser's
        // real CLI, --help, --version, --screenshot and all. Python stops
        // reading its own options at `-m <module>`, so everything the user
        // typed lands in sys.argv untouched, including arguments that look
        // like flags.
        let mut argv: Vec<Vec<u16>> = vec![
            wide(exe.as_os_str()),
            wide_str("-X"),
            wide_str("utf8"),
            wide_str("-m"),
            wide_str("feetbrowser"),
        ];
        argv.extend(std::env::args_os().skip(1).map(|a| wide(&a)));

        // `argv` owns the strings; `pointers` is the `wchar_t **` view of it
        // that Py_Main wants. Both stay alive until the process exits, which
        // is the point -- the interpreter keeps referring to argv the whole
        // time it runs.
        let mut pointers: Vec<*mut u16> = argv.iter_mut().map(|a| a.as_mut_ptr()).collect();
        let code = unsafe { py_main(pointers.len() as c_int, pointers.as_mut_ptr()) };
        std::process::exit(code);
    }
}
