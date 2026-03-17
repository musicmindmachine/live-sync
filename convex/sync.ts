import { internal } from "./_generated/api";
import { internalMutation, mutation, query } from "./_generated/server";
import { v } from "convex/values";

const COMPACTION_TRIGGER_OP_COUNT = 600;
const COMPACTION_RETAIN_OP_COUNT = 200;
const COMPACTION_BATCH_SIZE = 200;
const SNAPSHOT_FALLBACK_GAP = 400;
const MISSING = Symbol("missing");

type OperationRecord = {
  opId: string;
  clientId: string;
  lamport: number;
  kind: "set" | "delete";
  path: string;
  valueJson?: string;
};

type ClockEntry = {
  lamport: number;
  clientId: string;
  opId: string;
  kind: "set" | "delete";
};

type ClockMap = Record<string, ClockEntry>;

const operationValidator = v.object({
  opId: v.string(),
  clientId: v.string(),
  lamport: v.number(),
  kind: v.union(v.literal("set"), v.literal("delete")),
  path: v.string(),
  valueJson: v.optional(v.string()),
});

function decodePointer(path: string): string[] {
  if (path === "" || path === "/") {
    return [];
  }
  return path
    .replace(/^\//, "")
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
}

function encodePointer(segments: string[]): string {
  if (segments.length === 0) {
    return "";
  }
  return "/" + segments.map((segment) => segment.replace(/~/g, "~0").replace(/\//g, "~1")).join("/");
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value));
}

function pointerPrefixes(path: string): string[] {
  const segments = decodePointer(path);
  if (segments.length === 0) {
    return [""];
  }

  const prefixes = [""];
  for (let index = 1; index <= segments.length; index += 1) {
    prefixes.push(encodePointer(segments.slice(0, index)));
  }
  return prefixes;
}

function pointerDepth(path: string): number {
  return decodePointer(path).length;
}

function isDescendantPath(candidate: string, parent: string): boolean {
  const candidateSegments = decodePointer(candidate);
  const parentSegments = decodePointer(parent);

  if (parentSegments.length === 0) {
    return candidateSegments.length > 0;
  }

  if (candidateSegments.length <= parentSegments.length) {
    return false;
  }

  for (let index = 0; index < parentSegments.length; index += 1) {
    if (candidateSegments[index] !== parentSegments[index]) {
      return false;
    }
  }
  return true;
}

function compareClockEntries(left: ClockEntry, right: ClockEntry): number {
  if (left.lamport !== right.lamport) {
    return left.lamport < right.lamport ? -1 : 1;
  }
  if (left.clientId !== right.clientId) {
    return left.clientId < right.clientId ? -1 : 1;
  }
  if (left.opId !== right.opId) {
    return left.opId < right.opId ? -1 : 1;
  }
  return 0;
}

function getJsonValue(state: any, path: string): any | typeof MISSING {
  if (path === "" || path === "/") {
    return cloneJson(state);
  }

  let cursor = state;
  for (const segment of decodePointer(path)) {
    if (Array.isArray(cursor)) {
      const index = Number(segment);
      if (index >= cursor.length) {
        return MISSING;
      }
      cursor = cursor[index];
      continue;
    }

    if (cursor === null || typeof cursor !== "object" || !(segment in cursor)) {
      return MISSING;
    }
    cursor = cursor[segment];
  }
  return cloneJson(cursor);
}

function setJsonValue(state: any, path: string, value: any): any {
  if (path === "" || path === "/") {
    return cloneJson(value);
  }

  const root = Array.isArray(state) || (state !== null && typeof state === "object") ? cloneJson(state) : {};
  const segments = decodePointer(path);
  let cursor: any = root;

  for (let index = 0; index < segments.length - 1; index += 1) {
    const segment = segments[index];
    const nextSegment = segments[index + 1];
    const expectsList = /^\d+$/.test(nextSegment);

    if (Array.isArray(cursor)) {
      const listIndex = Number(segment);
      while (cursor.length <= listIndex) {
        cursor.push(expectsList ? [] : {});
      }
      if (
        cursor[listIndex] === null ||
        cursor[listIndex] === undefined ||
        (typeof cursor[listIndex] !== "object" && !Array.isArray(cursor[listIndex]))
      ) {
        cursor[listIndex] = expectsList ? [] : {};
      }
      cursor = cursor[listIndex];
      continue;
    }

    if (
      !(segment in cursor) ||
      cursor[segment] === null ||
      (typeof cursor[segment] !== "object" && !Array.isArray(cursor[segment]))
    ) {
      cursor[segment] = expectsList ? [] : {};
    }
    cursor = cursor[segment];
  }

  const finalSegment = segments[segments.length - 1];
  if (Array.isArray(cursor)) {
    const listIndex = Number(finalSegment);
    while (cursor.length <= listIndex) {
      cursor.push(null);
    }
    cursor[listIndex] = cloneJson(value);
    return root;
  }

  cursor[finalSegment] = cloneJson(value);
  return root;
}

function deleteJsonValue(state: any, path: string): any {
  if (path === "" || path === "/") {
    return {};
  }

  if (!Array.isArray(state) && (state === null || typeof state !== "object")) {
    return {};
  }

  const root = cloneJson(state);
  const segments = decodePointer(path);
  let cursor: any = root;

  for (const segment of segments.slice(0, -1)) {
    if (Array.isArray(cursor)) {
      const index = Number(segment);
      if (index >= cursor.length) {
        return root;
      }
      cursor = cursor[index];
      continue;
    }

    if (!(segment in cursor)) {
      return root;
    }
    cursor = cursor[segment];
  }

  const finalSegment = segments[segments.length - 1];
  if (Array.isArray(cursor)) {
    const index = Number(finalSegment);
    if (index < cursor.length) {
      cursor.splice(index, 1);
    }
    return root;
  }

  delete cursor[finalSegment];
  return root;
}

function applyOperationToState(state: any, operation: OperationRecord): any {
  if (operation.kind === "set") {
    const value = operation.valueJson === undefined ? null : JSON.parse(operation.valueJson);
    return setJsonValue(state, operation.path, value);
  }
  return deleteJsonValue(state, operation.path);
}

function parseClockJson(clockJson: string | undefined): ClockMap {
  if (!clockJson) {
    return {};
  }
  return JSON.parse(clockJson);
}

function serializeClockJson(clockMap: ClockMap): string {
  return JSON.stringify(clockMap);
}

function applyLwwOperation(state: any, clockMap: ClockMap, operation: OperationRecord) {
  const nextClockMap = cloneJson(clockMap);
  const opClock: ClockEntry = {
    lamport: operation.lamport,
    clientId: operation.clientId,
    opId: operation.opId,
    kind: operation.kind,
  };

  let winningPrefixClock: ClockEntry | null = null;
  for (const prefix of pointerPrefixes(operation.path)) {
    const existing = nextClockMap[prefix];
    if (!existing) {
      continue;
    }
    if (!winningPrefixClock || compareClockEntries(existing, winningPrefixClock) > 0) {
      winningPrefixClock = existing;
    }
  }

  if (winningPrefixClock && compareClockEntries(winningPrefixClock, opClock) >= 0) {
    return {
      state: cloneJson(state),
      clockMap: nextClockMap,
      applied: false,
    };
  }

  const preservedDescendants = Object.entries(nextClockMap)
    .filter(([path, clock]) => isDescendantPath(path, operation.path) && compareClockEntries(clock, opClock) > 0)
    .map(([path, clock]) => ({
      path,
      clock: cloneJson(clock),
      value: clock.kind === "set" ? getJsonValue(state, path) : MISSING,
    }))
    .sort((left, right) => {
      const depthDelta = pointerDepth(left.path) - pointerDepth(right.path);
      if (depthDelta !== 0) {
        return depthDelta;
      }
      return left.path.localeCompare(right.path);
    });

  let nextState = applyOperationToState(state, operation);

  for (const path of Object.keys(nextClockMap)) {
    if (isDescendantPath(path, operation.path) && compareClockEntries(nextClockMap[path], opClock) <= 0) {
      delete nextClockMap[path];
    }
  }
  nextClockMap[operation.path] = cloneJson(opClock);

  for (const descendant of preservedDescendants) {
    if (descendant.clock.kind === "set") {
      if (descendant.value === MISSING) {
        continue;
      }
      nextState = setJsonValue(nextState, descendant.path, descendant.value);
    } else {
      nextState = deleteJsonValue(nextState, descendant.path);
    }
    nextClockMap[descendant.path] = cloneJson(descendant.clock);
  }

  return {
    state: nextState,
    clockMap: nextClockMap,
    applied: true,
  };
}

async function ensureRoom(ctx: any, roomId: string) {
  let room = await ctx.db
    .query("rooms")
    .withIndex("by_room_id", (q: any) => q.eq("roomId", roomId))
    .unique();

  if (!room) {
    const now = Date.now();
    const roomDocId = await ctx.db.insert("rooms", {
      roomId,
      nextSequence: 1,
      stateJson: "{}",
      clockJson: "{}",
      compactedThroughSequence: 0,
      liveOpCount: 0,
      compactionScheduled: false,
      maxLamport: 0,
      mediaVersion: 0,
      mediaUpdatedAt: now,
      updatedAt: now,
    });
    room = await ctx.db.get(roomDocId);
  }

  if (!room) {
    throw new Error(`Failed to initialize room ${roomId}`);
  }

  const needsHydration =
    room.stateJson === undefined ||
    room.clockJson === undefined ||
    room.compactedThroughSequence === undefined ||
    room.liveOpCount === undefined ||
    room.compactionScheduled === undefined ||
    room.maxLamport === undefined ||
    room.mediaVersion === undefined ||
    room.mediaUpdatedAt === undefined;

  if (needsHydration) {
    const hydrated = {
      stateJson: room.stateJson ?? "{}",
      clockJson: room.clockJson ?? "{}",
      compactedThroughSequence: room.compactedThroughSequence ?? 0,
      liveOpCount: room.liveOpCount ?? 0,
      compactionScheduled: room.compactionScheduled ?? false,
      maxLamport: room.maxLamport ?? 0,
      mediaVersion: room.mediaVersion ?? 0,
      mediaUpdatedAt: room.mediaUpdatedAt ?? room.updatedAt ?? Date.now(),
    };
    await ctx.db.patch(room._id, hydrated);
    return {
      ...room,
      ...hydrated,
    };
  }

  return room;
}

async function scheduleCompactionIfNeeded(ctx: any, room: any, liveOpCount: number) {
  if (liveOpCount <= COMPACTION_TRIGGER_OP_COUNT || room.compactionScheduled) {
    return false;
  }
  await ctx.scheduler.runAfter(0, internal.sync.compactRoomLog, { roomId: room.roomId });
  return true;
}

export const pushOps = mutation({
  args: {
    roomId: v.string(),
    clientId: v.string(),
    ops: v.array(operationValidator),
  },
  handler: async (ctx, args) => {
    const room = await ensureRoom(ctx, args.roomId);
    const now = Date.now();
    let nextSequence = room.nextSequence;
    let liveOpCount = room.liveOpCount;
    let maxLamport = room.maxLamport ?? 0;
    let state = JSON.parse(room.stateJson ?? "{}");
    let clockMap = parseClockJson(room.clockJson);
    const accepted: Array<{ opId: string; sequence?: number; duplicate: boolean; applied: boolean }> = [];
    let hadAppliedOps = false;

    for (const rawOp of args.ops) {
      const op: OperationRecord = {
        ...rawOp,
        clientId: args.clientId,
      };

      const existing = await ctx.db
        .query("operations")
        .withIndex("by_room_op_id", (q) => q.eq("roomId", args.roomId).eq("opId", op.opId))
        .unique();

      if (existing) {
        accepted.push({
          opId: existing.opId,
          sequence: existing.sequence,
          duplicate: true,
          applied: true,
        });
        maxLamport = Math.max(maxLamport, existing.lamport);
        continue;
      }

      maxLamport = Math.max(maxLamport, op.lamport);
      const merged = applyLwwOperation(state, clockMap, op);
      if (!merged.applied) {
        accepted.push({
          opId: op.opId,
          duplicate: false,
          applied: false,
        });
        continue;
      }

      const sequence = nextSequence;
      nextSequence += 1;
      liveOpCount += 1;
      hadAppliedOps = true;
      state = merged.state;
      clockMap = merged.clockMap;

      await ctx.db.insert("operations", {
        roomId: args.roomId,
        sequence,
        opId: op.opId,
        clientId: op.clientId,
        lamport: op.lamport,
        kind: op.kind,
        path: op.path,
        valueJson: op.valueJson,
        createdAt: now,
      });

      accepted.push({
        opId: op.opId,
        sequence,
        duplicate: false,
        applied: true,
      });
    }

    const compactionScheduled = hadAppliedOps
      ? await scheduleCompactionIfNeeded(ctx, room, liveOpCount)
      : false;

    await ctx.db.patch(room._id, {
      nextSequence,
      stateJson: JSON.stringify(state),
      clockJson: serializeClockJson(clockMap),
      liveOpCount,
      compactionScheduled: room.compactionScheduled || compactionScheduled,
      maxLamport,
      updatedAt: hadAppliedOps ? now : room.updatedAt,
    });

    return {
      roomId: args.roomId,
      accepted,
      lastSequence: nextSequence - 1,
      snapshotJson: JSON.stringify(state),
      clockJson: serializeClockJson(clockMap),
      maxLamport,
    };
  },
});

export const pullOps = query({
  args: {
    roomId: v.string(),
    afterSequence: v.number(),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    const room = await ctx.db
      .query("rooms")
      .withIndex("by_room_id", (q) => q.eq("roomId", args.roomId))
      .unique();

    if (!room) {
      return {
        roomExists: false,
        latestSequence: 0,
        compactedThroughSequence: 0,
        resetRequired: false,
        snapshotSequence: 0,
        snapshotJson: undefined,
        clockJson: undefined,
        maxLamport: 0,
        ops: [],
      };
    }

    const latestSequence = room.nextSequence - 1;
    const compactedThroughSequence = room.compactedThroughSequence ?? 0;
    const stateJson = room.stateJson ?? "{}";
    const clockJson = room.clockJson ?? "{}";
    const maxLamport = room.maxLamport ?? 0;
    const limit = Math.min(Math.max(args.limit ?? 200, 1), 1000);
    const gap = Math.max(0, latestSequence - args.afterSequence);

    if (args.afterSequence < compactedThroughSequence || gap > SNAPSHOT_FALLBACK_GAP) {
      return {
        roomExists: true,
        latestSequence,
        compactedThroughSequence,
        resetRequired: true,
        snapshotSequence: latestSequence,
        snapshotJson: stateJson,
        clockJson,
        maxLamport,
        ops: [],
      };
    }

    const rows = await ctx.db
      .query("operations")
      .withIndex("by_room_sequence", (q) => q.eq("roomId", args.roomId).gt("sequence", args.afterSequence))
      .order("asc")
      .take(limit);

    return {
      roomExists: true,
      latestSequence,
      compactedThroughSequence,
      resetRequired: false,
      snapshotSequence: latestSequence,
      snapshotJson: undefined,
      clockJson: undefined,
      maxLamport,
      ops: rows.map((row) => ({
        sequence: row.sequence,
        opId: row.opId,
        clientId: row.clientId,
        lamport: row.lamport,
        kind: row.kind,
        path: row.path,
        valueJson: row.valueJson,
      })),
    };
  },
});

export const watchRoomVersion = query({
  args: {
    roomId: v.string(),
  },
  handler: async (ctx, args) => {
    const room = await ctx.db
      .query("rooms")
      .withIndex("by_room_id", (q) => q.eq("roomId", args.roomId))
      .unique();

    return {
      roomExists: room !== null,
      latestSequence: room ? room.nextSequence - 1 : 0,
      compactedThroughSequence: room ? (room.compactedThroughSequence ?? 0) : 0,
      maxLamport: room ? (room.maxLamport ?? 0) : 0,
      mediaVersion: room ? (room.mediaVersion ?? 0) : 0,
      updatedAt: room ? room.updatedAt : 0,
    };
  },
});

export const compactRoomNow = mutation({
  args: {
    roomId: v.string(),
  },
  handler: async (ctx, args) => {
    const room = await ensureRoom(ctx, args.roomId);
    let compactionScheduled = room.compactionScheduled ?? false;
    if (!compactionScheduled && room.liveOpCount > COMPACTION_RETAIN_OP_COUNT) {
      await ctx.scheduler.runAfter(0, internal.sync.compactRoomLog, { roomId: args.roomId });
      await ctx.db.patch(room._id, {
        compactionScheduled: true,
        updatedAt: Date.now(),
      });
      compactionScheduled = true;
    }
    return {
      roomId: args.roomId,
      compactionScheduled,
      liveOpCount: room.liveOpCount,
      compactedThroughSequence: room.compactedThroughSequence,
      maxLamport: room.maxLamport,
    };
  },
});

export const compactRoomLog = internalMutation({
  args: {
    roomId: v.string(),
  },
  handler: async (ctx, args) => {
    const room = await ctx.db
      .query("rooms")
      .withIndex("by_room_id", (q) => q.eq("roomId", args.roomId))
      .unique();

    if (!room) {
      return {
        roomId: args.roomId,
        deleted: 0,
        compactedThroughSequence: 0,
        liveOpCount: 0,
      };
    }

    const liveOpCount = room.liveOpCount ?? 0;
    const currentCompactedThroughSequence = room.compactedThroughSequence ?? 0;
    const excess = liveOpCount - COMPACTION_RETAIN_OP_COUNT;
    if (excess <= 0) {
      await ctx.db.patch(room._id, {
        compactionScheduled: false,
        updatedAt: Date.now(),
      });
      return {
        roomId: args.roomId,
        deleted: 0,
        compactedThroughSequence: currentCompactedThroughSequence,
        liveOpCount,
      };
    }

    const rows = await ctx.db
      .query("operations")
      .withIndex("by_room_sequence", (q) => q.eq("roomId", args.roomId))
      .order("asc")
      .take(Math.min(excess, COMPACTION_BATCH_SIZE));

    if (rows.length === 0) {
      await ctx.db.patch(room._id, {
        liveOpCount: 0,
        compactionScheduled: false,
        updatedAt: Date.now(),
      });
      return {
        roomId: args.roomId,
        deleted: 0,
        compactedThroughSequence: currentCompactedThroughSequence,
        liveOpCount: 0,
      };
    }

    for (const row of rows) {
      await ctx.db.delete(row._id);
    }

    const compactedThroughSequence = Math.max(currentCompactedThroughSequence, rows[rows.length - 1].sequence);
    const remainingLiveOpCount = Math.max(0, liveOpCount - rows.length);
    const shouldContinue = remainingLiveOpCount > COMPACTION_RETAIN_OP_COUNT;

    if (shouldContinue) {
      await ctx.scheduler.runAfter(0, internal.sync.compactRoomLog, { roomId: args.roomId });
    }

    await ctx.db.patch(room._id, {
      compactedThroughSequence,
      liveOpCount: remainingLiveOpCount,
      compactionScheduled: shouldContinue,
      updatedAt: Date.now(),
    });

    return {
      roomId: args.roomId,
      deleted: rows.length,
      compactedThroughSequence,
      liveOpCount: remainingLiveOpCount,
    };
  },
});

export const resetRoom = mutation({
  args: {
    roomId: v.string(),
  },
  handler: async (ctx, args) => {
    const room = await ctx.db
      .query("rooms")
      .withIndex("by_room_id", (q) => q.eq("roomId", args.roomId))
      .unique();

    const rows = await ctx.db
      .query("operations")
      .withIndex("by_room_sequence", (q) => q.eq("roomId", args.roomId))
      .collect();

    for (const row of rows) {
      await ctx.db.delete(row._id);
    }

    if (room) {
      await ctx.db.patch(room._id, {
        nextSequence: 1,
        stateJson: "{}",
        clockJson: "{}",
        compactedThroughSequence: 0,
        liveOpCount: 0,
        compactionScheduled: false,
        maxLamport: 0,
        mediaVersion: 0,
        mediaUpdatedAt: Date.now(),
        updatedAt: Date.now(),
      });
    }

    return {
      roomId: args.roomId,
      deleted: rows.length,
    };
  },
});
