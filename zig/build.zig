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
    b.step("test", "Run the engine's own tests").dependOn(&run_tests.step);
}
