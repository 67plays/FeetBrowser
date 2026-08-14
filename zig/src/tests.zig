//! Aggregates the engine's own test files so `zig build test` runs them all.

test {
    _ = @import("regex_test.zig");
    _ = @import("parser_test.zig");
    _ = @import("engine_test.zig");
}
