# The Zig JavaScript engine

FeetBrowser has a JavaScript engine written from scratch in Zig, loaded into
Python through a C ABI. This document is the design: what it does, what it
deliberately does not do, and — at length, because it is the part most likely
to go wrong — how it manages memory.

It is not a port of anything. The research behind it was mostly V8, because
V8 is the engine that documents itself best, but the conclusion of that
research was that most of what makes V8 V8 is a response to constraints we do
not have. What follows says, for each mechanism, what it buys and whether we
bought it.

## What we are optimising for

A page script is small, runs once, and spends most of its time talking to the
DOM. There is no steady state to warm up into. The largest single script on a
page we care about is a few hundred kilobytes of minified library code, most
of which never executes.

That shapes everything. V8's numbers make the point better than argument
does: with `--jitless`, V8 loses about 40% on Speedometer — and half of that
loss is the regular-expression interpreter, not the missing compiler — while a
real application, the YouTube living-room app, loses 6%. A browser engine
without a JIT is not the compromise it sounds like. A browser engine with a
slow regular-expression matcher is.

So: no JIT, no tiering, no deoptimisation, no on-stack replacement. A real
backtracking regular-expression engine with a step budget, written properly,
first.

## Value representation

A value is a 16-byte tagged union:

```zig
pub const Value = union(enum) {
    undefined, null,
    boolean: bool,
    number: f64,
    string: *Str,
    object: *Obj,
};
```

The two alternatives were NaN boxing and small-integer tagging, and we took
neither.

**NaN boxing** packs everything into eight bytes by hiding payloads in the
~2^51 bit patterns that are quiet NaNs but that no FPU ever produces.
SpiderMonkey, JavaScriptCore, LuaJIT and Duktape all do it, and it is a real
win: half the memory per value, and doubles cost nothing at all. It costs a
pointer restricted to 47 or 48 bits — so the heap must live in the low address
space, which has caused LuaJIT genuine trouble on AArch64 — and it costs the
absolute discipline of canonicalising every NaN that arithmetic produces,
because a computed NaN that is not canonicalised aliases a tagged pointer.
That is not a theoretical hazard; it is an exploited one.

We declined it because Zig gives us a checked tagged union for free. In debug
and safe builds a type confusion is a panic on the line that caused it, not a
corrupted pointer three frames later. At our heap sizes — a page's script
graph, not a long-lived application's — the memory we would save is not
memory we are short of. QuickJS made the same call for its 64-bit build for a
related reason: a 16-byte value fits in two registers and returns from a
function without touching memory.

**Small-integer tagging** — V8's SMI, where the low bit distinguishes a
31- or 32-bit integer from a pointer — exists so that an integer can avoid
being boxed on the heap. A union that already carries eight bytes of payload
never boxes one, so there is nothing to win. The entire apparatus that hangs
off SMIs in V8 (field representation tracking, `MutableHeapNumber`, double
field unboxing, map deprecation and lazy migration) exists so that optimised
code can assume unboxed doubles. We have no optimised code.

Strings are immutable UTF-8, collected, and never interned. There is no rope,
so a script that builds a megabyte one character at a time is quadratic; real
page scripts do not, and a rope needs flattening rules at every site that
wants bytes.

## Object model

An object is a class tag, a prototype pointer, an insertion-ordered property
map, and a variant payload:

```zig
pub const Obj = struct {
    gc: Gc,
    class: Class,       // plain, array, function, error, date, regexp, map, set, promise, host, …
    proto: ?*Obj,
    props: PropMap,
    data: Data,         // elements, entries, promise state, regex program, host handle, …
    extensible: bool,
};
```

**No hidden classes and no inline caches.** This is the largest deliberate
divergence from V8, and it deserves the argument rather than a shrug.

Hidden classes come from Chambers, Ungar and Lee's 1989 Self paper, where
they are called maps. Self is prototype-based, so naively every object carries
its own table of slot names — catastrophic space overhead. Maps group objects
cloned from the same prototype so the shared, immutable part (the names, the
offsets) is stored once and the object holds only values. It is worth being
clear that this is a *memory* technique that happens to also supply the type
descriptor a compiler wants; it pays off with no compiler at all.

Inline caching is Deutsch and Schiffman, 1984, resting on what they named
dynamic locality of type usage: at a given site, operand types tend to stay
constant. Hölzle, Chambers and Ungar extended it to several cached entries per
site in 1991, and measured a median 11% — the lasting contribution was that a
polymorphic inline cache is a type-feedback database, which is what every
optimising compiler since has consumed. The evidence that caching pays off
*without* a compiler is also direct: Brunthaler measured up to 1.71× on
CPython 3.1, and CPython 3.11's specialising interpreter reports 25–50%.

So we are leaving real speed on the table, and we know roughly how much. What
we are avoiding is the invalidation problem. Every cached shape-to-offset pair
needs invalidating on prototype mutation, on `delete`, and on any shape
change. V8 needs a `prototype_validity_cell` per prototype chain, a
transition tree with back pointers, a dictionary-mode fallback for objects
that fall off the fast path, a map-deprecation and lazy-migration scheme, and
a two-level global stub cache of 4096 plus 1024 entries for sites that go
megamorphic. Its own blog documents a case where the split-map logic
mishandled an extensibility transition and produced tens of thousands of
orphaned maps in React, destroying the entire benefit. That is where the bugs
live, and a bug there is a wrong answer, not a slow one.

An insertion-ordered array with a hash index into it is O(1) for lookup, is
the authority for the ordering that `Object.keys`, `for...in` and
`JSON.stringify` all promise, and cannot be wrong. The two things we did take
from the shape literature are cheap and unconditional: property keys are owned
byte copies so the collector never has to trace a property name, and array
indices never become properties at all — they live in a separate dense
element store, which is the same reason V8 keeps integer-indexed properties
out of its descriptor arrays.

Arrays are a dense `ArrayListUnmanaged(Value)`. There is one element kind, not
V8's twenty-one. Holes are stored as `undefined`, which is observably the same
thing for everything we implement — we have no `Object.prototype[0]`, so the
prototype-chain lookup that makes a hole expensive in V8 has nothing to find.

## Execution: bytecode, and why

We compile to a stack-machine bytecode and interpret that. The alternative was
walking the AST directly, which for an engine this size is a genuinely
reasonable choice — Larose and Marr's 2023 measurements found AST interpreters
on par with or slightly faster than bytecode ones under meta-compilation, and
V8's own motivation for Ignition was memory and re-parsing, not interpreter
speed. Three things decided it for us, and only one of them is speed.

**The operand stack is the root set.** This is the reason. At an instruction
boundary, every live value is in the value stack, in a frame, in the globals,
in a queued job, in a suspended coroutine, or in the embedder's handle table.
Nothing is stranded in a Zig local. A tree-walking interpreter holds live
values in Zig locals at every recursion level, which means a precise collector
needs a shadow stack maintained by hand at every site that can allocate — and
a missed entry there is a use-after-free that shows up once a week on one
page. With a bytecode VM the invariant is structural: collect only at the top
of the dispatch loop and precision is free.

**Suspension is cheap.** `await` has to put a half-finished function
somewhere. In a tree-walker that means either real coroutines with their own
stacks or a CPS transformation of the whole interpreter. Here it is a `memcpy`
of the frame's slice of the operand stack plus a program counter. That is the
entire implementation of `async`/`await` in this engine.

**The parse arena goes away.** The AST is written once and read once, so it
lives in an arena that is freed as soon as the function it belongs to has been
compiled. Nothing in the tree is collected, nothing in it is reference
counted, and a page that hands us 300 KB of minified library does not keep a
tree of fifty-variant tagged-union nodes alive for the life of the tab.

We took the stack machine rather than V8's accumulator-plus-register-file.
Ignition's register machine is smaller — a binary operation names one operand
instead of three — and the register file is a slice of the call frame, so
locals need no shuffling. But the encoding win is a code-size win, and code
size is what Ignition existed to fix (full-codegen's machine code was 15–20%
of the entire JS heap). Our bytecode is a rounding error next to the page's
own source. What we would lose by taking the register file is exactly the
property we chose bytecode for: with a register file the live set is the
frame's registers *plus* an accumulator held in a machine register, and the
accumulator is not somewhere the collector can see without being told.

Encoding is one opcode byte and fixed-width little-endian operands. No `Wide`
prefixes, no `Star0`–`Star15` short forms, no immediate-operand variants of
the arithmetic ops. Those buy density we do not need and cost decoding we
would notice.

Dispatch is a `switch` in a loop over a budget of instructions, with the
collection check hoisted out of the loop to the top of each budget slice.
Threaded dispatch via Zig's labelled `switch` with `continue :sw` would remove
one shared indirect branch in favour of one per handler, which Ertl and Gregg
measured as worth up to 2× on the hardware of 2003; modern branch predictors
have narrowed that a great deal, and the change is mechanical if we ever want
it.

## Scopes and closures

Every binding lives in a heap-allocated `Env`:

```zig
pub const Env = struct { gc: Gc, parent: ?*Env, slots: []Value, ready: []bool };
```

The compiler resolves every identifier at compile time to a `(depth, slot)`
pair, so a variable read is a chain walk of a statically known length and an
array index — never a name lookup. Identifiers that resolve to nothing become
global-by-name accesses, which is also how `var` at the top level and
undeclared assignment both end up on the global object.

A closure is a function object holding a pointer to the `Env` it was created
in. That is the whole mechanism. Escape analysis would let most scopes live on
the stack and would be a real win; it would also mean that a bug in the
analysis shows up as a variable that silently stops updating, which is the
worst class of bug this engine could have.

The `ready` array is temporal dead zone bookkeeping: `let` and `const` slots
start not-ready so a read before the declaration throws instead of quietly
seeing `undefined`. Per-iteration `let` bindings in a `for` loop are a
`copy_scope` instruction that clones the loop's scope at the top of each
iteration, which is what makes the classic "three closures capturing the same
`i`" case do the right thing.

`finally` is compiled by duplicating the finaliser's body at every exit path —
normal fall-through, `return`, each `break`, each `continue`, and the
exceptional path. That is more bytecode than a subroutine-return scheme, and
it is the reason there is no separate finally-return stack to get wrong.

## Memory management

This is the part that does not show up as a test failure. It shows up as a
leak, or as a use-after-free on a page nobody thought to test.

JavaScript makes cycles constantly and unavoidably — `function f() { ... }`
plus `f.prototype.constructor === f` is a cycle before the script has done
anything. The DOM makes worse ones: an event listener closure that captures
the element it is attached to forms a cycle through the listener registry, and
that is not an unusual pattern, it is the normal one.

Three options were on the table.

**An arena per script run** was rejected first. It is by far the simplest
thing that could work — allocate everything in an arena, drop the arena when
the script finishes — and it is wrong for us for a specific reason: our
scripts do not finish. A page installs a `click` handler and a `setTimeout`
and returns, and the engine has to keep that closure and everything it
captured alive across arbitrarily many later turns of the browser's event
loop. An arena that lives as long as the tab is a leak with a nice name.

**Reference counting with a cycle collector** is what QuickJS and Duktape do,
and it is genuinely attractive here for one reason above all others: QuickJS's
manual states it plainly — "the cycle removal algorithm only uses the
reference counts and the object content, so no explicit garbage collection
roots need to be manipulated in the C code." No handle scopes, no shadow
stack, no conservative scan. For an engine whose native side is a large
handwritten stdlib, that ergonomic property is worth a great deal. The second
attraction is determinism: QuickJS resets its collection threshold to 1.5×
live, where a pure tracer like mujs runs at 5× live, and for a browser holding
decoded images that ratio is not a footnote.

We declined it on three grounds. First, cost: a store becomes test-tag,
load-header, decrement, branch, maybe-free, test-tag, load-header, increment.
The ALU work is not the problem; the problem is that the increment *writes* to
the header of an object whose payload you may never read, pulling and dirtying
a cache line you would otherwise never touch. Shahriyar, Blackburn and
Frampton measured a well-engineered reference counter at 30% slower than a
well-tuned tracer, and closed the gap only with deferred and coalescing
schemes — both of which reintroduce a root scan and so give back precisely the
ergonomic property that made reference counting attractive.

Second, and decisively: **you pay for both.** QuickJS's cycle collector is
mechanically a two-pass trace over the entire heap that uses reference counts
as its mark state; Duktape's is a plain mark-sweep. Either way you must write
and maintain the full child-visitor for every type — which is exactly the
visitor a pure tracing collector needs, and nothing else. The incremental cost
of bolting reference counting on top is a word per object, a read-modify-write
on every pointer store, free-cascade work lists so a dropped subtree does not
blow the C stack, and the standing obligation that every path through the
native code be exactly balanced or it leaks silently. The only thing bought is
precise root enumeration — which the bytecode design already gives us for
free.

Third, "reference counting has no pauses" is not true. A single decrement can
cascade through an arbitrarily large acyclic subgraph, and QuickJS's cycle
pass walks the whole object list calling `mark_children` on everything.

**So: non-moving, precise mark and sweep**, over one intrusive linked list of
everything ever allocated.

```zig
pub const Gc = struct { next: ?*Gc = null, kind: GcKind, marked: bool = false };
```

Non-moving is the load-bearing adjective. A moving collector — V8's Cheney
scavenger for the young generation, its evacuating compactor for the old —
buys allocation as a pointer bump and a young-generation cost proportional to
survivors rather than to garbage, which given how JavaScript allocates is a
real prize. It costs forwarding pointers, a full pointer-fixup pass,
remembered sets, a write barrier on every store, and — the part that would
sink us — **the entire handle abstraction imposed on every line of embedder
code.** V8 cannot hand a C++ caller an object address, because the address is
stale the moment anything allocates; so it hands out `Local<T>`, which is
physically a `T**` pointing into a V8-owned slot that the collector rewrites
in place, and every embedder function needs a `HandleScope`, and a loop
without an inner one is the single most common V8 embedding leak. We are the
embedder, in two languages, across a C ABI. A raw `*Obj` that stays valid
forever is worth more to this project than bump allocation.

Not being generational is the borderline call. The weak generational
hypothesis holds strongly for JavaScript and the temptation is real, but
adding generations means owing a write barrier on every pointer store *even
though nothing moves*, plus a remembered set, plus the discipline never to
miss a barrier — and a missed barrier is a silent use-after-free. The
threshold policy (collect when the heap doubles past the last live size, floor
1 MB) is the cheap approximation, and the honest answer is that we should add
generations when a profile demands it and not before.

### The rooting rule

There is one rule, and everything else follows from it.

> A collection may only begin at the top of the dispatch loop.

At that point the root set is complete and enumerable: the value stack up to
`sp`, every frame's function, environment, `this`, `arguments` object and
result promise, the globals object, the built-in prototypes, the pending
microtask queue, the timer queue, every suspended coroutine's saved stack, the
embedder's handle table, and every compiled function's constant pool. `collect`
marks exactly those and sweeps. Marking uses an explicit grey worklist rather
than recursion, because a linked list built in JavaScript would otherwise
recurse as deep as the list is long.

The rule has exactly one class of exception, and it is the one place where
this design can bite. A native builtin that calls back into JavaScript —
`map`, `filter`, `reduce`, `sort`, a promise reaction, a `JSON.stringify`
replacer — re-enters the dispatch loop and therefore re-enters a safe point.
Any `Value` such a builtin is holding in a Zig local across that call is
invisible to the collector. Those builtins, and only those, must park their
temporaries in `Vm.temps`, which is a root. This is written down here because
it is not enforced by the type system and never will be; the mitigation is
that the set of such builtins is small, closed, and listed.

### The DOM boundary

The hard case in any browser engine is the listener cycle:

```
JS closure → captured element wrapper → host object → listener registry → JS closure
```

Give the JS heap to a tracing collector and the DOM to reference counting, and
neither side can free that: each, reaching the boundary, must conservatively
treat the other as a root. Chrome solved it by making both collectors
cooperate in a single marking pass (wrapper tracing, then Oilpan); WebKit
solved it by refusing to have two collectors and using opaque roots instead;
Gecko implements Bacon–Rajan trial deletion literally, with a `Traverse` and
an `Unlink` method that every participating class must get right.

We sidestep it, because our DOM is not in our heap — it is a Python object
graph, and Python has its own cycle collector. A Python object crossing into
JavaScript becomes an integer handle wrapped in a `Class.host` object; when
that wrapper is swept, a `host_release` callback tells Python to drop the
handle, and Python's own collector takes it from there. A JavaScript value
crossing into Python becomes an index into a handle table that is a GC root,
wrapped in a Python object whose `__del__` frees the slot. The cycle above
therefore has one edge — closure to wrapper — inside our heap, and the rest of
it inside Python's, and each collector sees a chain rather than a loop.

That is not free: a cycle whose edges alternate between the two heaps is
retained by both, exactly as it would be between any two independent tracing
collectors. In practice the shape that matters (a listener closure holding an
element) is retained by the Python node tree anyway for as long as the node is
in the document, and both sides drop it when the tab's interpreter is torn
down. It is a known and bounded limitation rather than a solved problem, and
it is recorded as such in `docs/limitations.md`.

Strings across the boundary are copied immediately in both directions and
never borrowed past the call that produced them. Handles are the only thing
with a lifetime, and there are exactly two tables — one on each side.

## The interface

The engine is a dynamic library exporting a C ABI, loaded with `ctypes`. Not a
Python extension module: `feetbrowser/cocoa.py` and `feetbrowser/win32.py`
already call into the operating system exactly this way, and a `ctypes` build
has no dependency on the Python version it will be loaded by, needs no
`maturin`, and needs no virtual environment.

Callbacks go the other way through `ctypes.CFUNCTYPE`, which is how the DOM
works: the DOM objects operate on the Python node tree that layout renders, so
a property read on `document.body` is a call out of Zig, into Python, and
back. The boundary is five function pointers — get, set, call, construct,
release — and one 24-byte wire struct.

## Scope

Implemented: closures, `var`/`let`/`const` with a temporal dead zone, objects,
classes with `extends` and `super`, getters and setters, arrays with index
growth and `length` truncation, all the loop forms with labelled `break` and
`continue`, `try`/`catch`/`finally`/`throw`, arrow functions with lexical
`this`, template literals and tagged templates, spread and rest, destructuring
with defaults, optional chaining, nullish coalescing, `Promise` with a real
microtask queue, `async`/`await`, timers, and the operator set with JavaScript's
coercion rules. Builtins: `Object`, `Array`, `String`, `Number`, `Boolean`,
`Math`, `JSON`, `Map`, `Set`, `Date`, `RegExp`, `Error` and its subclasses,
`console`, `parseInt`, `parseFloat`, plus the host-provided `fetch` and
`XMLHttpRequest`.

Not implemented, and why, is in `docs/limitations.md`.
