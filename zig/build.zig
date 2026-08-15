const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseFast });

    const mod = b.createModule(.{
        .root_source_file = b.path("src/capi.zig"),
        .target = target,
        .optimize = optimize,
    });

    const lib = b.addLibrary(.{
        .name = "feetjs",
        .root_module = mod,
        .linkage = .dynamic,
    });
    // The browser loads this by path with ctypes, so the file lands beside the
    // sources rather than in a versioned install tree.
    const install = b.addInstallArtifact(lib, .{ .dest_dir = .{ .override = .{ .custom = "lib" } } });
    b.getInstallStep().dependOn(&install.step);

    const tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/tests.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run the engine's own tests");
    test_step.dependOn(&run_tests.step);

    // The parser and regex suites are standalone programs rather than `test`
    // blocks, because both count their own cases and the parser one doubles as
    // a tree dumper you can hand a snippet. `zig build test` has to build and
    // run them itself; importing them the way tests.zig imports engine_test
    // would compile them and run nothing.
    for ([_][]const u8{ "src/parser_test.zig", "src/regex_test.zig" }) |path| {
        const exe = b.addExecutable(.{
            .name = std.fs.path.stem(path),
            .root_module = b.createModule(.{
                .root_source_file = b.path(path),
                .target = target,
                .optimize = optimize,
            }),
        });
        test_step.dependOn(&b.addRunArtifact(exe).step);
    }
}
