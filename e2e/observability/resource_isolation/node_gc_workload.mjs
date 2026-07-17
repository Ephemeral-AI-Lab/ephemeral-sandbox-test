import { closeSync, openSync, writeSync } from "node:fs";
import { monitorEventLoopDelay, PerformanceObserver } from "node:perf_hooks";

const destination = process.argv[2];
const durationMs = Number(process.argv[3]);
const arm = process.argv[4];
const repetition = Number(process.argv[5]);
const loadMultiplier = Number(process.argv[6] ?? 1);

if (
  !destination ||
  !Number.isFinite(durationMs) ||
  durationMs < 1 ||
  !arm ||
  !Number.isInteger(loadMultiplier) ||
  loadMultiplier < 1
) {
  throw new Error(
    "usage: node_gc_workload.mjs OUTPUT DURATION_MS ARM REPETITION LOAD_MULTIPLIER",
  );
}

const descriptor = openSync(destination, "w");
const started = performance.now();
let sequence = 0;
let peakRss = 0;
let allocationTicks = 0;
let allocatedArrays = 0;
let forcedGcCount = 0;

function emit(record) {
  const line = JSON.stringify({
    schema_version: 1,
    arm,
    repetition,
    sequence: sequence++,
    elapsed_ms: performance.now() - started,
    ...record,
  });
  if (Buffer.byteLength(line) + 1 > 16 * 1024) {
    throw new Error("GC evidence line exceeded 16 KiB");
  }
  writeSync(descriptor, `${line}\n`);
}

const gcObserver = new PerformanceObserver((list) => {
  for (const entry of list.getEntries()) {
    emit({
      type: "gc",
      duration_ms: entry.duration,
      kind: entry.detail?.kind ?? null,
      flags: entry.detail?.flags ?? null,
    });
  }
});
gcObserver.observe({ entryTypes: ["gc"] });

const delay = monitorEventLoopDelay({ resolution: 10 });
delay.enable();
const live = [];

// Keep one deterministic 10-ms cadence and scale logical sub-batches. Trimming
// after each sub-batch bounds the live set while a 10x profile performs exactly
// ten times the configured allocation and explicit-GC work per timer callback.
const allocationIntervalMs = 10;
const allocationBatchesPerTick = loadMultiplier;
const allocationsPerBatch = 8;
const gcEveryAllocationTicks = 50;
const allocator = setInterval(() => {
  for (let batch = 0; batch < allocationBatchesPerTick; batch += 1) {
    for (let index = 0; index < allocationsPerBatch; index += 1) {
      live.push(new Array(8192).fill(allocationTicks + index));
    }
    allocatedArrays += allocationsPerBatch;
    while (live.length > 96) {
      live.splice(0, Math.min(live.length - 96, 32));
    }
    allocationTicks += 1;
    if (allocationTicks % gcEveryAllocationTicks === 0 && global.gc) {
      global.gc();
      forcedGcCount += 1;
    }
  }
}, allocationIntervalMs);

const sampler = setInterval(() => {
  const memory = process.memoryUsage();
  peakRss = Math.max(peakRss, memory.rss);
  emit({
    type: "sample",
    rss_bytes: memory.rss,
    heap_used_bytes: memory.heapUsed,
    heap_total_bytes: memory.heapTotal,
    event_loop_delay_p99_ms: delay.percentile(99) / 1e6,
  });
  delay.reset();
}, 100);

await new Promise((resolve) => setTimeout(resolve, durationMs));
clearInterval(allocator);
clearInterval(sampler);
live.length = 0;
if (global.gc) {
  global.gc();
}
await new Promise((resolve) => setTimeout(resolve, 50));
const finalMemory = process.memoryUsage();
peakRss = Math.max(peakRss, finalMemory.rss);
delay.disable();
gcObserver.disconnect();
emit({
  type: "summary",
  allocation_ticks: allocationTicks,
  allocated_arrays: allocatedArrays,
  forced_gc_count: forcedGcCount,
  load_multiplier: loadMultiplier,
  allocation_interval_ms: allocationIntervalMs,
  allocation_batches_per_tick: allocationBatchesPerTick,
  allocations_per_batch: allocationsPerBatch,
  gc_every_allocation_ticks: gcEveryAllocationTicks,
  peak_rss_bytes: peakRss,
  final_rss_bytes: finalMemory.rss,
  oom: false,
});
closeSync(descriptor);
