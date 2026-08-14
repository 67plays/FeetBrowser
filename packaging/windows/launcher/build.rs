//! Compile the Win32 resource script and link it into FeetBrowser.exe.
//!
//! An icon, a version block and an application manifest are not decoration:
//! Explorer shows the icon, `Get-Item ... | % VersionInfo` is what an admin
//! reads to find out what a binary claims to be, and without a manifest the
//! process gets whatever defaults Windows hands an unmarked executable.
//!
//! The usual way to do this is the `winres`/`embed-resource` crate. This does
//! it with `rc.exe` from the Windows SDK directly instead -- it is a dozen
//! lines either way, and the launcher's dependency list stays empty, which is
//! the one thing about this project worth protecting. `.res` files are a
//! linker input that MSVC's `link.exe` accepts by path, so the whole
//! integration is one `rustc-link-arg`.

use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=resources/FeetBrowser.rc");
    println!("cargo:rerun-if-changed=resources/FeetBrowser.ico");
    println!("cargo:rerun-if-changed=resources/FeetBrowser.manifest");
    println!("cargo:rerun-if-env-changed=FEETBROWSER_REQUIRE_RESOURCES");

    let target = env::var("TARGET").unwrap_or_default();
    if !target.contains("windows-msvc") {
        return;
    }

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    let rc_source = manifest_dir.join("resources").join("FeetBrowser.rc");
    let res_output = out_dir.join("FeetBrowser.res");

    let rc = match find_rc() {
        Some(rc) => rc,
        None => return give_up("no rc.exe found (install the Windows SDK)"),
    };

    // rc.exe resolves the paths inside the script relative to the script, so
    // the .ico and the .manifest are found without any /I of our own. It also
    // runs a C preprocessor, which is why the script includes no headers:
    // every constant in it is written as a bare number.
    let status = Command::new(&rc)
        .arg("/nologo")
        .arg("/fo")
        .arg(&res_output)
        .arg(&rc_source)
        .status();

    match status {
        Ok(s) if s.success() => {}
        Ok(s) => return give_up(&format!("{} exited {}", rc.display(), s)),
        Err(e) => return give_up(&format!("could not run {}: {}", rc.display(), e)),
    }

    println!("cargo:rustc-link-arg-bins={}", res_output.display());
}

/// A missing SDK is a warning for a developer poking at the launcher and an
/// error for the build that produces the thing users download. `build.ps1`
/// sets the variable; nothing else does.
fn give_up(why: &str) {
    if env::var_os("FEETBROWSER_REQUIRE_RESOURCES").is_some() {
        panic!("FeetBrowser.exe would ship with no icon or version info: {}", why);
    }
    println!("cargo:warning=building FeetBrowser.exe without icon or version info: {}", why);
}

/// Locate `rc.exe`.
///
/// On PATH already inside a Visual Studio developer prompt; otherwise under
/// the SDK, one directory per SDK version, newest last after a sort.
fn find_rc() -> Option<PathBuf> {
    if Command::new("rc.exe").arg("/?").output().is_ok() {
        return Some(PathBuf::from("rc.exe"));
    }
    let arch = if env::var("TARGET").unwrap_or_default().starts_with("aarch64") {
        "arm64"
    } else {
        "x64"
    };
    let mut roots = Vec::new();
    for var in ["ProgramFiles(x86)", "ProgramFiles"] {
        if let Some(p) = env::var_os(var) {
            roots.push(PathBuf::from(p).join("Windows Kits").join("10").join("bin"));
        }
    }
    let mut candidates = Vec::new();
    for root in roots {
        let entries = match std::fs::read_dir(&root) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let candidate = entry.path().join(arch).join("rc.exe");
            if candidate.is_file() {
                candidates.push(candidate);
            }
        }
        // Older SDK layouts put rc.exe straight in bin/<arch>.
        let flat = root.join(arch).join("rc.exe");
        if flat.is_file() {
            candidates.push(flat);
        }
    }
    candidates.sort();
    candidates.pop()
}
